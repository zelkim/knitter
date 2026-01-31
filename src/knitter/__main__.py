import argparse
import logging
import json
import os
import sys
import shutil
import subprocess
import tomllib

from pathlib import Path
from typing import Any

import jinja2
from jinja2 import meta
import livereload
import rcssmin


# It's dirty, but I'm putting this here since we're moving the entire tool
# into its own project anyway so this is fine. For now. I think. Maybe. Oh
# gosh, the overthinking is setting in!
def is_dict(var):
    # Based on: https://stackoverflow.com/a/11947595/1116098
    return isinstance(var, dict)


def is_list(var):
    return isinstance(var, list)


template_loader: jinja2.FileSystemLoader = jinja2.FileSystemLoader('.')
jinja_env: jinja2.Environment = jinja2.Environment(
    loader=template_loader,
    lstrip_blocks=True,
    trim_blocks=True)
jinja_env.tests['dict'] = is_dict
jinja_env.tests['list'] = is_list

def _load_config_and_data() -> tuple[dict[str, Any], dict[str, Any]]:
    """Load knitter.toml and associated JSON data files.

    This is intentionally lazy so that commands like `knitter init` can run
    in a directory that does not yet have a configuration file.
    """

    with open('knitter.toml', 'rb') as f:
        config_data: dict[str, Any] = tomllib.load(f)

    loaded_data: dict[str, Any] = {}

    data_config: dict[str, Any] = config_data.get('data', {})
    for name, data in data_config.items():
        with open(data, encoding='utf-8') as f:
            loaded_data[name] = json.load(f)

            is_data_table: bool = (
                'headings' in loaded_data[name]
                and (
                    'rows' in loaded_data[name]['headings']
                    and 'columns' in loaded_data[name]['headings']
                )
            )
            if is_data_table:
                if 'focused_data_columns' in loaded_data[name]:
                    data_list: list = []
                    for row in loaded_data[name]['data']:
                        for i, col in enumerate(row):
                            # Since the first element is the first column.
                            idx = i + 1
                            if idx in loaded_data[name]['focused_data_columns']:
                                data_list.append(col)
                else:
                    data_list: list = sum(loaded_data[name]['data'], start=[])

                loaded_data[name]['max_data_val'] = max(data_list)

    return config_data, loaded_data


class BuildException(Exception):
    pass


def _create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog='Knitter',
        description='A static site generator originally built for use in the development of the Dev8 website.')
    parser.add_argument('task', choices=['build', 'serve', 'init'])
    parser.add_argument('directory', nargs='?', help='Target directory for init (e.g., "." or "my-website").')

    return parser


def _setup_logger(name: str):
    logger: logging.Logger = logging.getLogger(name)
    logger.setLevel(logging.DEBUG)

    # Create handler to log to console.
    console_handler: logging.StreamHandler = logging.StreamHandler()
    console_formatter: logging.Formatter = logging.Formatter('%(message)s')
    console_handler.setFormatter(console_formatter)
    console_handler.setLevel(logging.DEBUG)

    logger.addHandler(console_handler)

    # Create handler to log to file.
    file_handler: logging.FileHandler = logging.FileHandler('knitter.log')

    file_format: str = '%(asctime)s: %(name)-18s [%(levelname)-8s] %(message)s'
    file_formatter: logging.Formatter = logging.Formatter(file_format)

    file_handler.setFormatter(file_formatter)
    file_handler.setLevel(logging.INFO)

    logger.addHandler(file_handler)

    return logger


def _build(base_dist_dir: Path = Path('dist'), mode: str = 'production'):
    config_data, loaded_data = _load_config_and_data()

    os.makedirs(base_dist_dir, exist_ok=True)

    # Set up routes.
    for route, html_file in config_data['routes'].items():
        template: jinja2.Template = jinja_env.get_template(html_file)
        contents: str = f'{template.render(**loaded_data)}\n'

        route: str = route.strip('/')  # Having '/' at the start messes up Path.

        dest: Path = base_dist_dir / route / 'index.html'

        dest.parent.mkdir(exist_ok=True, parents=True)

        with open(dest, 'w', encoding='utf-8') as f:
            f.write(contents)

    # Preprocess files.
    assets_dir: Path = base_dist_dir / 'assets'
    for processed_file, src_file in config_data['processed_files'].items():
        # Create the files first in the dist/ folder.
        target_path: Path = assets_dir / processed_file
        target_path.parent.mkdir(exist_ok=True, parents=True)

        # Preprocess via SASS.
        try:
            executable: str = 'sass'
            if sys.platform.startswith('win32'):
                executable: str = 'sass.bat'

            subprocess.run([executable, src_file, target_path])
        except FileNotFoundError as e:
            err_msg: str = ('sass not found. Make sure Sass is '
                            'installed and in your PATH. Build cancelled')
            raise BuildException(err_msg)

        if mode == 'production':
            # Minify.
            with open(target_path, encoding='utf-8') as css_file:
                css_src: str = css_file.read()

            minified_css: str = rcssmin.cssmin(css_src)
            with open(target_path, 'w', encoding='utf-8') as css_file:
                css_file.write(minified_css)

    # Copy assets.
    src_assets_dir: Path = Path(config_data['assets_folder']['folder'])
    shutil.copytree(src_assets_dir, assets_dir, dirs_exist_ok=True)


def _serve(base_dist_dir: Path = Path('dist')):
    config_data, _ = _load_config_and_data()

    logger: logging.Logger = logging.getLogger('knitter')

    try:
        _build(base_dist_dir)
    except BuildException as e:
        logger.warning(f'{e}. Previously built files will be served.')

    # Set up watch list.
    watch_list: list[str] = []
    for html_file in config_data['routes'].values():
        watch_list.append(html_file)

        with open(html_file, encoding='utf-8') as f:
            contents: str = f.read()

        ast: jinja2.Template = jinja_env.parse(contents)
        referenced_templates: list = list(meta.find_referenced_templates(ast))
        for template in referenced_templates:
            watch_list.append(template)

    for processed_file in config_data['processed_files'].values():
        watch_list.append(processed_file)

        nested_imports: set[Path] = _find_scss_imports(Path(processed_file))
        for path in nested_imports:
            watch_list.append(os.path.normpath(str(path)))

    assets_folder_path: str = config_data['assets_folder']['folder']
    assets_folder_path = assets_folder_path.strip('/')
    assets_folder_path += '/**/*.*'
    watch_list.append(assets_folder_path)

    # Set up basic server.
    def build():
        _build(base_dist_dir, 'development')

    server: livereload.Server = livereload.Server()
    for path in watch_list:
        delay: int | None = None
        if '.scss' in path:
            delay = 5

        server.watch(path, build, delay=delay)

    host: str = 'localhost'
    port: int = 2016
    root: str = 'dist/'

    logger.info(f'Starting server at http://{host}:{port}...')
    server.serve(host=host, port=port, root=root)


def _init(base_dir: Path):
    """Initialize a new Knitter project in the given directory.

    This will create a basic directory structure, a knitter.toml file,
    starter templates, styles, JavaScript, and a locale JSON file used by
    the default landing page.
    """

    logger: logging.Logger = logging.getLogger('knitter')

    config_path: Path = base_dir / 'knitter.toml'
    if config_path.exists():
        logger.error('knitter.toml already exists. Aborting init to avoid overwriting your configuration.')
        raise SystemExit(1)

    # Determine where the starter template lives inside this package.
    template_root: Path = Path(__file__).resolve().parent / 'project_template'
    if not template_root.exists():
        logger.error('Project template directory not found inside Knitter package.')
        raise SystemExit(1)

    # Copy all files from the template into the target directory, without
    # overwriting any existing files.
    for dirpath, dirnames, filenames in os.walk(template_root):
        source_dir: Path = Path(dirpath)
        relative_dir: Path = source_dir.relative_to(template_root)
        target_dir: Path = base_dir / relative_dir
        target_dir.mkdir(parents=True, exist_ok=True)

        for filename in filenames:
            src_path: Path = source_dir / filename
            dest_path: Path = target_dir / filename

            if dest_path.exists():
                logger.info(f'Skipping existing file: {dest_path}')
                continue

            shutil.copy2(src_path, dest_path)

    logger.info(f'Initialized a new Knitter project in: {base_dir.resolve()}')


def _find_scss_imports(starting_file: Path) -> set[Path]:
    imported_files: set[Path] = set()
    with open(starting_file, encoding='utf-8') as f:
        for line in f.readlines():
            if '@import' in line:
                imported_file: str = line.replace('@import', '')
                imported_file = imported_file.strip().strip(';')
                imported_file = imported_file.strip('\'').strip('\"')
                imported_file: Path = starting_file.parent / imported_file
                imported_files.add(imported_file)

                # DIG DEEPERRR! RAAGHHH! RECURSION TIME!
                imported_files.update(_find_scss_imports(Path(imported_file)))

    return imported_files


def main():
    logger: logging.Logger = _setup_logger('knitter')

    parser: argparse.ArgumentParser = _create_parser()
    args: argparse.Namespace = parser.parse_args()

    if args.task == 'build':
        try:
            _build()
        except BuildException as e:
            logger.error(f'{str(e)}.')
            sys.exit(127)
    elif args.task == 'serve':
        _serve()
    elif args.task == 'init':
        target_dir_arg = getattr(args, 'directory', None)

        if not target_dir_arg:
            logger.error('No directory specified. \nUsage: knitter init <directory> (e.g., "knitter init my-website" or "knitter init .").')
            sys.exit(2)

        base_dir = Path(target_dir_arg)
        _init(base_dir)
