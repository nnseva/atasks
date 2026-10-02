"""Generate lightweight references for tasks declared in a Python module."""
import argparse
import ast
import os
import sys


_DECORATOR_REFS = {
    'atask': 'ataskref',
    'atask_queue': 'atask_qref',
    'atask_broadcast': 'atask_bref',
}


def _decorator_info(decorator):
    """Return the matching reference factory and literal decorator options."""
    if isinstance(decorator, ast.Name):
        decorator_name = decorator.id
        keywords = []
    elif isinstance(decorator, ast.Call) and isinstance(decorator.func, ast.Name):
        decorator_name = decorator.func.id
        keywords = decorator.keywords
    else:
        return None

    ref_factory = _DECORATOR_REFS.get(decorator_name)
    if ref_factory is None:
        return None

    options = {}
    for keyword in keywords:
        if keyword.arg in ('name', 'namespace'):
            if not isinstance(keyword.value, ast.Constant) or not isinstance(keyword.value.value, str):
                raise ValueError('%s= must be a string literal' % keyword.arg)
            options[keyword.arg] = keyword.value.value
    return ref_factory, options


def _task_references(source, module_name):
    """Extract task reference definitions from Python ``source``."""
    tree = ast.parse(source)
    references = []
    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        for decorator in node.decorator_list:
            decorator_info = _decorator_info(decorator)
            if decorator_info is None:
                continue
            ref_factory, options = decorator_info
            task_name = options.get('name', '%s.%s' % (module_name, node.name))
            namespace = options.get('namespace')
            references.append((node.name, ref_factory, task_name, namespace))
            break
    return references


def _default_module_name(source_path):
    """Derive the import name for ``source_path`` relative to the current directory."""
    relative_path = os.path.relpath(source_path, os.getcwd())
    stem, extension = os.path.splitext(relative_path)
    if extension != '.py' or stem.startswith('..' + os.sep) or stem == '..':
        raise ValueError('source must be a Python file below the current directory')
    parts = stem.split(os.sep)
    if parts[-1] == '__init__':
        parts.pop()
    if not parts or any(not part.isidentifier() for part in parts):
        raise ValueError('cannot derive a Python module name from %r' % source_path)
    return '.'.join(parts)


def _render_references(references):
    """Render extracted references as a standalone Python source file."""
    lines = ['from atasks.refs import atask_bref, atask_qref, ataskref', '']
    for function_name, ref_factory, task_name, namespace in references:
        factory = ref_factory
        if namespace is not None:
            factory += '(namespace=%r)' % namespace
        lines.append('%s = %s[%r]' % (function_name, factory, task_name))
    return '\n'.join(lines) + '\n'


def generate(source_path, module_name=None):
    """Generate a ``_refs.py`` sibling for ``source_path`` and return its path."""
    if not source_path.endswith('.py'):
        raise ValueError('source must have a .py extension')
    source_path = os.path.abspath(source_path)
    if module_name is None:
        module_name = _default_module_name(source_path)

    with open(source_path, encoding='utf-8') as source_file:
        references = _task_references(source_file.read(), module_name)

    output_path = source_path[:-3] + '_refs.py'
    with open(output_path, 'w', encoding='utf-8') as output_file:
        output_file.write(_render_references(references))
    return output_path


def main(argv=None):
    """Run the ``atasks refs`` command."""
    parser = argparse.ArgumentParser(
        prog=argv[0] if argv is not None else None,
        formatter_class=argparse.RawTextHelpFormatter,
        description='Generate a _refs.py module with lightweight references for tasks declared in SOURCE.',
    )
    parser.add_argument('source', help='Python module to inspect for @atask decorators')
    parser.add_argument(
        '--module',
        help='module name for tasks without name=; defaults to SOURCE relative to the current directory',
    )
    options = parser.parse_args(argv[1:] if argv is not None else None)
    try:
        generate(options.source, options.module)
    except (OSError, SyntaxError, ValueError) as error:
        parser.error(str(error))


if __name__ == '__main__':
    sys.path.insert(0, '.')
    main(sys.argv)
