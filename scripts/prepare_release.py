"""Validate and export an explicit source-only release; never publish or push."""
from __future__ import annotations
import argparse
from pathlib import Path
import re
from urllib.parse import unquote,urlsplit
import zipfile
from dotenv import dotenv_values

ROOT=Path(__file__).resolve().parents[1]
FILES=('README.md','CONTRIBUTING.md','LICENSE','.gitignore','.gitattributes','.env.example','requirements.txt','start.cmd','start.command')
DIRECTORIES=('geo_workbench','docs','examples','test-data','tests','scripts','.github')
SKIP_PARTS={'__pycache__','.pytest_cache','.git','.venv','.local-data','.local-archive','.release','history'}


def source_files(root=ROOT):
    root=Path(root).resolve();result=[]
    candidates=[root/name for name in FILES]
    for name in DIRECTORIES:
        if (root/name).is_dir():candidates.extend((root/name).rglob('*'))
    for path in candidates:
        relative=path.relative_to(root)
        if set(relative.parts)&SKIP_PARTS:continue
        if path.name.startswith('.env') and relative.as_posix()!='.env.example':continue
        if path.suffix.lower() in ('.pyc','.pyo','.sqlite','.sqlite3','.db','.log','.zip'):continue
        if path.is_symlink() or not path.resolve().is_relative_to(root):raise ValueError('发布目录含链接或越界路径：'+str(relative))
        if path.is_file():result.append(path)
    return sorted(set(result))


def validate(root=ROOT):
    root=Path(root).resolve();paths=source_files(root)
    names={p.relative_to(root).as_posix() for p in paths}
    example=root/'.env.example'
    if not example.is_file():raise ValueError('缺少 .env.example')
    sensitive=lambda name:bool(re.search(r'(api_key|token|password|secret)$',name,re.I))
    if any(value for key,value in dotenv_values(example).items() if sensitive(key)):
        raise ValueError('.env.example 中的凭证值必须为空')
    secrets=[v.encode() for k,v in dotenv_values(root/'.env').items() if sensitive(k) and v and len(v)>=8]
    for path in paths:
        relative=path.relative_to(root).as_posix();data=path.read_bytes()
        if any(secret in data for secret in secrets):raise ValueError('源码含本地凭证，请检查文件：'+relative)
        if path.suffix.lower()!='.md':continue
        for raw in re.findall(r'\]\(([^)]+)\)',data.decode('utf-8')):
            link=raw.strip().strip('<>')
            if link.startswith('#') or urlsplit(link).scheme in ('http','https','mailto','data'):continue
            target=(path.parent/unquote(link.split('#',1)[0])).resolve()
            if not target.is_relative_to(root):raise ValueError('文档链接越界：'+relative)
            name=target.relative_to(root).as_posix()
            if name not in names and not any(n.startswith(name.rstrip('/')+'/') for n in names):
                raise ValueError('文档链接不在发布清单：'+relative+' -> '+link)
    return paths


def write_zip(output,root=ROOT):
    root=Path(root).resolve();paths=validate(root);output=Path(output).resolve()
    if output in paths or output.name.startswith('.env'):raise ValueError('不能覆盖源码或环境文件')
    output.parent.mkdir(parents=True,exist_ok=True)
    with zipfile.ZipFile(output,'w',zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            relative=path.relative_to(root).as_posix();info=zipfile.ZipInfo(relative)
            info.create_system=3;info.external_attr=(0o100755 if path.suffix in ('.command','.sh') else 0o100644)<<16
            info.compress_type=zipfile.ZIP_DEFLATED
            archive.writestr(info,path.read_bytes())
    return len(paths)


def main(argv=None):
    parser=argparse.ArgumentParser(description='检查并打包纯源码，不执行 Git 或上传')
    parser.add_argument('--check',action='store_true')
    parser.add_argument('--output',type=Path,default=ROOT/'.release'/'geo-aigc-source.zip')
    args=parser.parse_args(argv)
    try:
        if args.check:print('发布检查通过：',len(validate()),'个源码／文档文件；未打包。')
        else:print('已打包：',write_zip(args.output),'个文件 →',args.output)
    except (ValueError,OSError) as exc:parser.exit(1,str(exc)+'\n')


if __name__=='__main__':main()
