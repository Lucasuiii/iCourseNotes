"""Conservative, pause-aware chunk planning for the isolated Qwen test."""
import math


def reaches_acquisition_limit(duration, limit=10800.0):
    """A hard ffmpeg cutoff may round just below its requested duration."""
    return not math.isfinite(duration) or duration >= limit - 0.1


def plan_chunks(windows, duration, maximum=28.0, padding=1.0):
    """Keep VAD pauses; merge adjacent speech only within the size limit.

    Continuous speech gets bounded chunks with at most two seconds overlap.
    Quiet speech is not rejected with an amplitude threshold.
    """
    if not math.isfinite(duration) or duration <= 0 or maximum <= 0 or padding < 0:
        raise ValueError("Invalid chunk limits")
    merged = []
    for start, end in sorted(windows):
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration):
            raise ValueError("Invalid speech window")
        if merged and start - merged[-1][1] <= 0.8 and end - merged[-1][0] <= maximum:
            merged[-1] = (merged[-1][0], max(end, merged[-1][1]))
        else:
            merged.append((start, end))
    chunks = []
    for start, end in merged:
        cursor = start
        while cursor < end:
            stop = min(cursor + maximum, end)
            chunks.append((max(0, cursor - padding), min(duration, stop + padding)))
            cursor = stop
    return chunks


def deduplicated_chunk_rows(rows):
    """Trim only exact substantial repeats across overlapping audio windows.

    Do not manufacture sentence timestamps; rows retain their audio intervals.
    Short repeated words may be deliberate, so keep them.
    """
    output = []
    previous = None
    for row in rows:
        text = row.get("text", "").strip()
        if previous and row["start"] < previous["end"]:
            prior = previous.get("text", "").strip()
            for size in range(min(len(prior), len(text), 120), 5, -1):
                if prior[-size:] == text[:size]:
                    text = text[size:]
                    break
        if text:
            output.append({**row, 'text': text})
        previous = row
    return output


def join_chunk_text(rows):
    return "\n".join(row['text'] for row in deduplicated_chunk_rows(rows))


def plan_long_chunks(windows, duration, target=120.0, silence_skip=10.0, padding=1.0):
    """Retain short pauses, skip long no-speech gaps, cut near two minutes.

    VAD supplies candidate pauses, not individual ASR requests. Use a pause
    within the final 30 seconds of a block, otherwise cut at the target.
    Padding gives at most two seconds overlap and target+2 seconds input.
    """
    if not all(math.isfinite(x) for x in (duration, target, silence_skip, padding)):
        raise ValueError("Invalid chunk limits")
    if duration <= 0 or target < 30 or silence_skip <= 0 or padding < 0:
        raise ValueError("Invalid chunk limits")
    regions, pauses = [], []
    for start, end in sorted(windows):
        if not (math.isfinite(start) and math.isfinite(end) and 0 <= start < end <= duration):
            raise ValueError("Invalid speech window")
        if regions and start - regions[-1][1] < silence_skip:
            if start - regions[-1][1] >= 0.5:
                pauses.append((regions[-1][1] + start) / 2)
            regions[-1] = (regions[-1][0], max(regions[-1][1], end))
        else:
            regions.append((start, end))
    chunks = []
    for start, end in regions:
        cursor = start
        while cursor < end:
            stop = min(cursor + target, end)
            if stop < end:
                candidates = [p for p in pauses if stop - 30 <= p <= stop and p > cursor]
                if candidates:
                    stop = candidates[-1]
            chunks.append((max(0, cursor - padding), min(duration, stop + padding)))
            cursor = stop
    return chunks


def split_inference_audio(samples, rate=16000, maximum=30.0):
    """Shared <=30s inference clips, split near the lowest midpoint energy.

    Offsets are exact sample indices. Clips neither overlap nor omit samples;
    the original two-minute checkpoint plan remains authoritative.
    """
    import numpy as np
    if not math.isfinite(rate) or not math.isfinite(maximum) or rate <= 0 or maximum <= 0:
        raise ValueError('Invalid inference audio limits')
    if len(samples) <= maximum*rate:
        return [(samples, 0)]
    window = max(1, round(.5*rate))
    positions = range(int(len(samples)*.2), int(len(samples)*.8)-window, max(1,window//2))
    position = min(positions, key=lambda start: float(np.mean(np.square(samples[start:start+window]))),
                   default=len(samples)//2-window//2)
    middle = max(1, min(len(samples)-1, position+window//2))
    left = split_inference_audio(samples[:middle], rate, maximum)
    right = split_inference_audio(samples[middle:], rate, maximum)
    return left + [(clip, start+middle) for clip,start in right]
