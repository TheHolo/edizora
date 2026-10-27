"""Export the actual FastAPI contract without booting runtime or using credentials."""
import argparse
import json
from pathlib import Path

from app.api.server import create_app


def run():
    parser=argparse.ArgumentParser()
    parser.add_argument('--output',type=Path,default=Path('docs/openapi.json'))
    options=parser.parse_args()
    # Route construction has no side effects and does not evaluate dependencies.
    app=create_app(None,None,deployment=object())
    schema=app.openapi()
    options.output.parent.mkdir(parents=True,exist_ok=True)
    options.output.write_text(json.dumps(schema,ensure_ascii=False,indent=2)+'\n',encoding='utf-8')
    print(f'Exported {len(schema["paths"])} actual API paths')


if __name__=='__main__':run()
