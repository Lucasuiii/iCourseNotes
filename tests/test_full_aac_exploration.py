import io
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from dataclasses import replace

from test_aac_ranges import fixture,MemoryReader,Origin
from src.runtime.aac_ranges import (AACRangeTransport,Limits,MediaTransportError,
    iter_track_packets,track_batches,load_index,stream_full_track)
from scripts.explore_full_aac import run_full


class FullAACExplorationTests(unittest.TestCase):
    def test_full_stream_final_duration_and_each_packet_once(self):
        data=fixture();seen=[]
        with Origin(data,'reorder') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            index=load_index(reader)
            result=stream_full_track(reader,index,lambda p,b:seen.append((p.number,b)))
            self.assertEqual(seen,[(0,b'abc'),(1,b'DEFG'),(2,b'hijkl')])
            self.assertEqual(result['verified_samples'],3);self.assertEqual(result['audio_payload_bytes'],12)
            self.assertTrue(result['full_packet_coverage'])
            self.assertEqual(list(iter_track_packets(index))[-1].duration,1008)

    def test_missing_part_commits_no_unverified_packet(self):
        data=fixture();seen=[]
        with Origin(data,'missing') as origin,AACRangeTransport(origin.client,origin.url) as reader:
            index=load_index(reader)
            with self.assertRaises(MediaTransportError):stream_full_track(reader,index,lambda p,b:seen.append(p.number))
            self.assertEqual(seen,[])

    def test_interrupted_batch_retries_do_not_duplicate_full_track(self):
        data=fixture();seen=[]
        with Origin(data) as origin,AACRangeTransport(origin.client,origin.url) as reader:
            index=load_index(reader);origin.mode='drop';origin.calls=1
            result=stream_full_track(reader,index,lambda p,b:seen.append(p.number))
            self.assertEqual(seen,[0,1,2]);self.assertEqual(result['verified_samples'],3)
            self.assertEqual(reader.statistics()['retries'],1)

    def test_batch_memory_and_partial_edit_bounds(self):
        index=load_index(MemoryReader(fixture()))
        self.assertEqual([len(batch) for batch in track_batches(index,max_packets=1)],[1,1,1])
        index.presentation_end=index.duration-1
        with self.assertRaises(MediaTransportError):list(iter_track_packets(index))
        with self.assertRaises(ValueError):list(track_batches(index,max_ranges=65))
        with self.assertRaises(ValueError):Limits(requests=10001)

    @unittest.skipUnless(shutil.which('ffmpeg') and shutil.which('ffprobe'),'FFmpeg required')
    def test_real_entire_track_reference_decode_and_owned_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            source=Path(tmp)/'source.mp4'
            subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=sample_rate=48000:duration=3',
                '-ac','2','-c:a','aac','-use_editlist','0',str(source)],check=True)
            with Origin(source.read_bytes()) as origin,AACRangeTransport(origin.client,origin.url) as reader:
                index=load_index(reader)
                with tempfile.TemporaryDirectory(prefix='aac-full-test-') as owned:
                    folder=Path(owned);evidence=run_full(reader,index,folder,lambda *a,**k:None)
                    self.assertEqual(evidence['reference_packets'],len(index.sizes))
                    self.assertTrue(evidence['all_packet_hashes_equal'])
                    self.assertTrue(evidence['all_native_pts_dts_durations_equal'])
                    self.assertEqual(evidence['mp4_decode']['decode_return_code'],0)
                    self.assertFalse(evidence['timeline_preserved'])
                self.assertFalse(folder.exists())
