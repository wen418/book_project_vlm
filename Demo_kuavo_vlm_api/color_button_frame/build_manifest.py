"""Build deployment provenance after tests; never run automatically at startup."""
import hashlib
import json
from pathlib import Path

ROOT=Path(__file__).resolve().parent


def main():
    files=[*ROOT.glob('*.py'),*ROOT.glob('*.sh'),*ROOT.joinpath('upstream').glob('*.py'),ROOT/'upstream/perception.json']
    manifest={'release':'color_button_frame_v2','button_only_replaced':True,
              'button_confirmation_frames':1,
              'door_backend':'original API','floor_backend':'original API',
              'lower_file_modified':False,
              'release_files_sha256':{f.relative_to(ROOT).as_posix():hashlib.sha256(f.read_bytes()).hexdigest() for f in files}}
    (ROOT/'manifest.json').write_text(json.dumps(manifest,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print('MANIFEST',len(files),'files')


if __name__=='__main__':main()
