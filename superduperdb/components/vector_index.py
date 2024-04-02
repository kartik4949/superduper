import dataclasses as dc
import typing as t

import numpy as np
from overrides import override

from superduperdb.base.datalayer import Datalayer
from superduperdb.base.document import Document
from superduperdb.components.component import Component
from superduperdb.components.datatype import DataType
from superduperdb.components.listener import Listener
from superduperdb.components.model import Mapping, ModelInputType
from superduperdb.ext.utils import str_shape
from superduperdb.jobs.job import FunctionJob
from superduperdb.misc.annotations import public_api
from superduperdb.misc.special_dicts import MongoStyleDict
from superduperdb.vector_search.base import VectorIndexMeasureType
from superduperdb.vector_search.update_tasks import copy_vectors

KeyType = t.Union[str, t.List, t.Dict]
if t.TYPE_CHECKING:
    from superduperdb.jobs.job import Job


@public_api(stability='stable')
@dc.dataclass(kw_only=True)
class VectorIndex(Component):
    """
    A component carrying the information to apply a vector index to a ``DB`` instance
    {component_parameters}
    :param indexing_listener: Listener which is applied to created vectors
    :param compatible_listener: Listener which is applied to vectors to be compared
    :param measure: Measure to use for comparison
    :param metric_values: Metric values for this index
    """

    ui_schema: t.ClassVar[t.List[t.Dict]] = [
        {'name': 'indexing_listener', 'type': 'component/listener'},
        {'name': 'compatible_listener', 'type': 'component/listener', 'optional': True},
        {'name': 'measure', 'type': 'str', 'choices': ['cosine', 'dot', 'l2']},
    ]

    __doc__ = __doc__.format(component_parameters=Component.__doc__)

    type_id: t.ClassVar[str] = 'vector_index'

    indexing_listener: Listener
    compatible_listener: t.Optional[Listener] = None
    measure: VectorIndexMeasureType = VectorIndexMeasureType.cosine
    metric_values: t.Optional[t.Dict] = dc.field(default_factory=dict)

    @override
    def on_load(self, db: Datalayer) -> None:
        if isinstance(self.indexing_listener, str):
            self.indexing_listener = t.cast(
                Listener, db.load('listener', self.indexing_listener)
            )

        if isinstance(self.compatible_listener, str):
            self.compatible_listener = t.cast(
                Listener, db.load('listener', self.compatible_listener)
            )

    def get_vector(
        self,
        like: Document,
        models: t.List[str],
        keys: KeyType,
        db: t.Any = None,
        outputs: t.Optional[t.Dict] = None,
    ):
        document = MongoStyleDict(like.unpack())
        if outputs is not None:
            outputs = outputs or {}
            if '_outputs' not in document:
                document['_outputs'] = {}
            document['_outputs'].update(outputs)
            assert not isinstance(self.indexing_listener, str)
        available_keys = list(document.keys())

        key: t.Optional[t.Any] = None
        model_name: t.Optional[str] = None
        for m, k in zip(models, keys):
            if isinstance(k, str):
                if k in available_keys:
                    model_name, key = m, k
            elif isinstance(k, (tuple, list)):
                if all([i in available_keys for i in list(k)]):
                    model_name, key = m, k
            elif isinstance(k, dict):
                if all([i in available_keys for i in k.values()]):
                    model_name, key = m, k

        if not key:
            try:
                assert isinstance(keys, list)
                kix = keys.index('_base')
                model_name, key = models[kix], keys[kix]
            except ValueError:
                raise Exception(
                    f'Keys in provided {like} don\'t match'
                    f'VectorIndex keys: {keys}, with model: {models}'
                )

        model = db.models[model_name]
        data = Mapping(key, model.signature)(document)
        args, kwargs = model.handle_input_type(data, model.signature)
        return (
            model.predict_one(*args, **kwargs),
            model.identifier,
            key,
        )

    def get_nearest(
        self,
        like: Document,
        db: t.Any,
        id_field: str = '_id',
        outputs: t.Optional[t.Dict] = None,
        ids: t.Optional[t.Sequence[str]] = None,
        n: int = 100,
    ) -> t.Tuple[t.List[str], t.List[float]]:
        """Given a document, find the nearest results in this vector index, returned as
        two parallel lists of result IDs and scores

        :param like: The document to compare against
        :param db: The datastore to use
        :param outputs: An optional dictionary
        :param ids: A list of ids to match
        :param n: Number of items to return
        """

        models, keys = self.models_keys
        if len(models) != len(keys):
            raise ValueError(f'len(model={models}) != len(keys={keys})')
        within_ids = ids or ()

        if isinstance(like, dict) and id_field in like:
            return db.fast_vector_searchers[self.identifier].find_nearest_from_id(
                str(like[id_field]), within_ids=within_ids, limit=n
            )
        h = self.get_vector(
            like=like,
            models=models,
            keys=keys,
            db=db,
            outputs=outputs,
        )[0]

        searcher = db.fast_vector_searchers[self.identifier]

        return searcher.find_nearest_from_array(h, within_ids=within_ids, n=n)

    @property
    def models_keys(self) -> t.Tuple[t.List[str], t.List[ModelInputType]]:
        """
        Return a list of model and keys for each listener
        """
        assert not isinstance(self.indexing_listener, str)
        assert not isinstance(self.compatible_listener, str)

        if self.compatible_listener:
            listeners = [self.indexing_listener, self.compatible_listener]
        else:
            listeners = [self.indexing_listener]

        models = [w.model.identifier for w in listeners]
        keys = [w.key for w in listeners]
        return models, keys

    @property
    def dimensions(self) -> int:
        assert not isinstance(self.indexing_listener, str)
        assert not isinstance(self.indexing_listener.model, str)
        if shape := getattr(self.indexing_listener.model.datatype, 'shape', None):
            return shape[-1]
        raise ValueError('Couldn\'t get shape of model outputs from model encoder')

    @override
    def schedule_jobs(
        self,
        db: Datalayer,
        dependencies: t.Sequence['Job'] = (),
    ) -> t.Sequence[t.Any]:
        """
        Schedule jobs for the listener

        :param database: The DB instance to process
        :param dependencies: A list of dependencies
        :param verbose: Whether to print verbose output
        """
        if not db.cdc.running:
            job = FunctionJob(
                callable=copy_vectors,
                args=[],
                kwargs={
                    'vector_index': self.identifier,
                    'ids': [],
                    'query': self.indexing_listener.select.dict().encode(),
                },
            )
            job(db, dependencies=dependencies)
            return [job]
        return []


class EncodeArray:
    def __init__(self, dtype):
        self.dtype = dtype

    def __call__(self, x, info: t.Optional[t.Dict] = None):
        x = np.asarray(x)
        if x.dtype != self.dtype:
            raise TypeError(f'dtype was {x.dtype}, expected {self.dtype}')
        return memoryview(x).tobytes()


class DecodeArray:
    def __init__(self, dtype):
        self.dtype = dtype

    def __call__(self, bytes, info: t.Optional[t.Dict] = None):
        return np.frombuffer(bytes, dtype=self.dtype).tolist()


def vector(shape):
    """
    Create an encoder for a vector (list of ints/ floats) of a given shape

    :param shape: The shape of the vector
    """
    return DataType(
        identifier=f'vector[{str_shape(shape)}]',
        shape=shape,
        encoder=None,
        decoder=None,
        encodable='native',
    )


def sqlvector(shape):
    """
    Create an encoder for a vector (list of ints/ floats) of a given shape
    compatible with sql databases.

    :param shape: The shape of the vector
    """
    return DataType(
        identifier=f'sqlvector[{str_shape(shape)}]',
        shape=shape,
        encoder=EncodeArray(dtype='float64'),
        decoder=DecodeArray(dtype='float64'),
    )
