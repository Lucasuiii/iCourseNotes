"""Encrypt a local glossary for the repository using DB_ENCRYPTION_KEY."""
import argparse
import json
import os
from pathlib import Path

from src.ai.course_glossary import encrypt_glossary, decrypt_glossary, ENCRYPTED_GLOSSARY_PATH


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('--output', type=Path, default=ENCRYPTED_GLOSSARY_PATH)
    args = parser.parse_args()
    entries = json.loads(args.input.read_text())
    password = os.environ.get('DB_ENCRYPTION_KEY', '')
    sealed = encrypt_glossary(entries, password)
    if decrypt_glossary(sealed, password) != entries:
        raise ValueError('Glossary encryption roundtrip failed')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix+'.tmp')
    try:
        with temporary.open('xb') as stream:
            stream.write(sealed)
        temporary.replace(args.output)
    finally:
        temporary.unlink(missing_ok=True)
    print('Encrypted glossary saved and verified.')


if __name__ == '__main__':
    main()
