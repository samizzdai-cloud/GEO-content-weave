import io
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile
from geo_workbench import images,research
from geo_workbench.__main__ import main,open_when_ready
from scripts import prepare_release


class Release(unittest.TestCase):
    def test_source_export_excludes_environment_data_archives_and_keeps_example(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)
            for name,body in {'.env':'DEEPSEEK_API_KEY=private-sentinel-123456789',
                              '.env.example':'DEEPSEEK_API_KEY=',
                              'README.md':'[Code](geo_workbench/app.py)',
                              'geo_workbench/app.py':'print("app")',
                              'geo_workbench/.env.dev':'PRIVATE=hidden',
                              'geo_workbench/__pycache__/app.pyc':'compiled',
                              '.local-archive/private.txt':'archive',
                              '.local-data/tasks.sqlite':'database',
                              'start.command':'#!/bin/sh\necho start'}.items():
                path=root/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(body,encoding='utf-8')
            out=root/'.release'/'source.zip';prepare_release.write_zip(out,root)
            with zipfile.ZipFile(out) as archive:
                self.assertEqual(set(archive.namelist()),{'.env.example','README.md','geo_workbench/app.py','start.command'})
                self.assertTrue(archive.getinfo('start.command').external_attr>>16&0o111)
            (root/'geo_workbench/app.py').write_text('private-sentinel-123456789',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'凭证'):prepare_release.validate(root)
            (root/'geo_workbench/app.py').write_text('valid',encoding='utf-8')
            (root/'.env.example').write_text('SEEDREAM_API_KEY=bad',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'必须为空'):prepare_release.validate(root)

    def test_published_links_cannot_target_excluded_local_archive(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);(root/'.env.example').write_text('DEEPSEEK_API_KEY=',encoding='utf-8')
            (root/'.local-archive').mkdir();(root/'.local-archive'/'old.md').write_text('old',encoding='utf-8')
            (root/'README.md').write_text('[private](.local-archive/old.md)',encoding='utf-8')
            with self.assertRaisesRegex(ValueError,'发布清单'):prepare_release.validate(root)

    def test_dotenv_is_loaded_before_store_data_directory(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);package=root/'geo_workbench';package.mkdir()
            source=Path(__file__).resolve().parents[1]/'geo_workbench'
            for name in ('__init__.py','store.py'):shutil.copy2(source/name,package/name)
            target=root/'custom-data';(root/'.env').write_text('GEO_DATA_DIR='+target.as_posix(),encoding='utf-8')
            env=os.environ.copy();env.pop('GEO_DATA_DIR',None)
            result=subprocess.run([sys.executable,'-c','from geo_workbench import store; print(store.ROOT)'],cwd=root,env=env,capture_output=True,text=True,check=True)
            self.assertEqual(Path(result.stdout.strip()).resolve(),target.resolve())

    def test_launcher_uses_single_local_worker_and_checks_port(self):
        with patch('uvicorn.run') as run:
            main(['--port','8899']);run.assert_called_once_with('geo_workbench.app:app',host='127.0.0.1',port=8899,workers=1)
        with patch('sys.stderr',new=io.StringIO()):
            with self.assertRaises(SystemExit):main(['--port','99999'])

    def test_browser_opens_after_readiness_without_proxying_localhost(self):
        with patch('geo_workbench.__main__.urllib.request.build_opener') as build,patch('geo_workbench.__main__.webbrowser.open') as browser:
            build.return_value.open.return_value.__enter__.return_value.status=200
            self.assertTrue(open_when_ready('http://127.0.0.1:8899/'))
            browser.assert_called_once_with('http://127.0.0.1:8899/')
            self.assertEqual(build.call_args.args[0].proxies,{})

    def test_direct_api_adapters_inherit_proxy_environment(self):
        with patch.dict(os.environ,{'DEEPSEEK_API_KEY':'fixture','DEEPSEEK_BASE_URL':'https://api.deepseek.com'}),patch('geo_workbench.research.httpx.Client') as client:
            client.return_value.__enter__.return_value.post.return_value.json.return_value={}
            research.Tools('fixture').post('/models',{})
            self.assertTrue(client.call_args.kwargs['trust_env'])
        with patch.dict(os.environ,{'SEEDREAM_API_KEY':'fixture'}),patch('geo_workbench.images.httpx.Client') as client:
            response=client.return_value.__enter__.return_value.post.return_value;response.status_code=200
            response.json.return_value={'data':[{'b64_json':'cG5n'}]}
            self.assertEqual(images.ImageAgent().generate({})[0],b'png')
            self.assertTrue(client.call_args.kwargs['trust_env'])

    def test_mac_font_selection_and_explicit_bad_font(self):
        mac='/System/Library/Fonts/PingFang.ttc'
        with patch.dict(os.environ,{'GEO_LABEL_FONT':''}),patch('geo_workbench.images.Path.is_file',lambda p:p.as_posix()==mac),patch('geo_workbench.images.ImageFont.truetype') as font:
            images.label_font(20);font.assert_called_once_with(mac,20)
        with patch.dict(os.environ,{'GEO_LABEL_FONT':'/missing-font.ttf'}),patch('geo_workbench.images.Path.is_file',return_value=False):
            with self.assertRaisesRegex(ValueError,'GEO_LABEL_FONT'):images.label_font(20)
