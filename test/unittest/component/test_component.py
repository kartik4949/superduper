import dataclasses as dc
import os
import shutil
import tempfile
import typing as t
from test.db_config import DBConfig

import pytest

from superduper import ObjectModel
from superduper.components.component import Component
from superduper.components.datatype import (
    Artifact,
    DataType,
    Empty,
    LazyArtifact,
    dill_serializer,
)
from superduper.components.listener import Listener
from superduper.ext.torch.encoder import tensor


@pytest.fixture
def cleanup():
    yield
    try:
        os.remove('test_export.tar.gz')
        shutil.rmtree('test_export')
    except FileNotFoundError:
        pass


@dc.dataclass(kw_only=True)
class MyComponent(Component):
    type_id: t.ClassVar[str] = 'my_type'
    _lazy_fields: t.ClassVar[t.Sequence[str]] = ('my_dict',)
    my_dict: t.Dict
    nested_list: t.List
    a: t.Callable


def test_init(monkeypatch):
    from unittest.mock import MagicMock

    e = Artifact(x=None, identifier='123', datatype=dill_serializer)
    a = Artifact(x=None, identifier='456', datatype=dill_serializer)

    def side_effect(*args, **kwargs):
        a.x = lambda x: x + 1

    a.init = MagicMock()
    a.init.side_effect = side_effect

    list_ = [e, a]

    c = MyComponent('test', my_dict={'a': a}, a=a, nested_list=list_)

    c.init()

    assert callable(c.my_dict['a'])
    assert c.my_dict['a'](1) == 2

    assert callable(c.a)
    assert c.a(1) == 2

    assert callable(c.nested_list[1])
    assert c.nested_list[1](1) == 2


@pytest.mark.parametrize("db", [DBConfig.mongodb], indirect=True)
def test_load_lazily(db):
    m = ObjectModel('lazy_model', object=lambda x: x + 2)

    db.add(m)

    reloaded = db.load('model', m.identifier)

    assert isinstance(reloaded.object, LazyArtifact)
    assert isinstance(reloaded.object.x, Empty)

    reloaded.init(db=db)

    assert callable(reloaded.object)


def test_export_and_read():
    m = ObjectModel(
        'test', object=lambda x: x + 2, datatype=tensor(dtype='float', shape=(32,))
    )

    with tempfile.TemporaryDirectory() as tmpdir:
        save_path = os.path.join(tmpdir, 'tmp_save')
        m.export(save_path)
        assert os.path.exists(os.path.join(tmpdir, 'tmp_save', 'blobs'))

        def load(blob):
            with open(blob, 'rb') as f:
                return f.read()

        reloaded = Component.read(save_path)  # getters=getters

        assert isinstance(reloaded, ObjectModel)
        assert isinstance(reloaded.datatype, DataType)


@pytest.mark.parametrize("db", [DBConfig.mongodb_empty], indirect=True)
def test_set_variables(db):
    m = Listener(
        model=ObjectModel(
            identifier='<var:test>',
            object=lambda x: x + 2,
        ),
        key='<var:key>',
        select=db['docs'].find(),
    )

    from superduper import Document

    e = m.encode()
    recon = Document.decode(e).unpack()

    recon.init(db=db)

    listener = m.set_variables(test='test_value', key='key_value', docs='docs_value')
    assert listener.model.identifier == 'test_value'
    assert listener.key == 'key_value'
