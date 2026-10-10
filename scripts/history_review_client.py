"""Local private key and comparison viewer; never accesses a network or database."""
import argparse
import base64
import json
import os
from pathlib import Path


def private_write(path, content):
    path = Path(path); path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'wb') as out: out.write(content)


def keygen(path):
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey
    private = X25519PrivateKey.generate()
    private_write(path, private.private_bytes(serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw, serialization.NoEncryption()))
    return base64.b64encode(private.public_key().public_bytes(
        serialization.Encoding.Raw, serialization.PublicFormat.Raw)).decode()


def comparison(input_path, private_path, source_run, output_path):
    from scripts.production_result_export import decrypt, MAX_EXPORT
    source = Path(input_path)
    if source.stat().st_size > MAX_EXPORT+65:
        raise ValueError('Encrypted comparison exceeds limit')
    report = json.loads(decrypt(source.read_bytes(), Path(private_path).read_bytes(), source_run, 0))
    if report['source_run'] != source_run:
        raise ValueError('Comparison source changed')
    rows = ['# 历史课程结果对照', '', '预览批次：'+source_run,
            '', '确认指纹：`'+report['approval_sha256']+'`', '',
            '检查转录缺段、关键术语、公式和作业信息后，再决定是否覆盖。', '']
    for lesson in report['lectures']:
        from src.pipeline.recognition_coverage import missing_recognition_notice
        status = ('新结果保留识别缺口，属于不完整转录。'
                  + missing_recognition_notice(lesson.get('recognition_coverage'))
                  if lesson.get('recognition_complete') is False else
                  '新结果已通过完整识别与复核门禁；此检查不保证文字没有错误。')
        rows.extend([f'## 课次 {lesson["sub_id"]} · {lesson["date"]}', '',
                     status, ''])
        for field, title in (('summary','摘要'),('transcript','转录')):
            for version, label in (('old','旧'),('new','新')):
                rows.extend(['### '+label+title, '', lesson[version][field], ''])
        rows.extend(['### 新结果复核记录', '', '```json',
                     json.dumps(lesson['review'], ensure_ascii=False, indent=2), '```', ''])
    private_write(output_path, '\n'.join(rows).encode())
    return report['approval_sha256']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest='operation', required=True)
    generate = commands.add_parser('keygen'); generate.add_argument('--private', default='.history-refresh/review.key')
    read = commands.add_parser('read'); read.add_argument('--input', required=True)
    read.add_argument('--source-run', required=True); read.add_argument('--private', default='.history-refresh/review.key')
    read.add_argument('--output', default='.history-refresh/comparison.md')
    args = parser.parse_args()
    if args.operation == 'keygen': print(keygen(args.private))  # Public key only.
    else: print('Review saved privately; approval SHA256: '+comparison(
        args.input, args.private, args.source_run, args.output))


if __name__ == '__main__': main()
