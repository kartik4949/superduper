import re


def _find_variables(r):
    from superduper.base.leaf import Leaf

    if isinstance(r, dict):
        return sum([_find_variables(v) for v in r.values()], [])
    if isinstance(r, (list, tuple)):
        return sum([_find_variables(v) for v in r], [])
    if isinstance(r, str):
        return re.findall(r'<var:(.*?)>', r)
    if isinstance(r, Leaf):
        return r.variables
    return []


def _replace_variables(x, **kwargs):
    from .document import Document
    if isinstance(x, dict):
        return {
            _replace_variables(k, **kwargs): _replace_variables(v, **kwargs)
            for k, v in x.items()
        }
    if isinstance(x, str) and re.match(r'^<var:(.*?)>$', x) is not None:
        return kwargs.get(x[5:-1], x)
    if isinstance(x, str):
        variables = re.findall(r'<var:(.*?)>', x)
        variables = list(map(lambda v: v.strip(), variables))
        for k, v in kwargs.items():
            if k in variables:
                if isinstance(v, str):
                    x = x.replace(f'<var:{k}>', v)
                else:
                    x = re.sub('[<>:]', '-', x)
                    x = re.sub('[-]+', '-', x)
        return x
    if isinstance(x, (list, tuple)):
        return [_replace_variables(v, **kwargs) for v in x]
    if isinstance(x, Document):
        return x.set_variables(**kwargs)
    return x
