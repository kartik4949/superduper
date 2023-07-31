import dataclasses as dc
import io
import typing as t
from contextlib import contextmanager
from functools import cached_property

import torch
from torch.utils import data
from torch.utils.data import DataLoader
from tqdm import tqdm

from superduperdb import logging
from superduperdb.container.artifact import Artifact
from superduperdb.container.document import Document
from superduperdb.container.encoder import Encodable
from superduperdb.container.metric import Metric
from superduperdb.container.model import Model, _TrainingConfiguration
from superduperdb.container.serializable import Serializable
from superduperdb.db.base.datalayer import Datalayer
from superduperdb.db.base.query import Select
from superduperdb.db.query_dataset import QueryDataset
from superduperdb.ext.torch.utils import device_of, eval, to_device


class BasicDataset(data.Dataset):
    """
    Basic database iterating over a list of documents and applying a transformation

    :param documents: documents
    :param transform: function
    """

    def __init__(self, documents, transform):
        super().__init__()
        self.documents = documents
        self.transform = transform

    def __len__(self):
        return len(self.documents)

    def __getitem__(self, item):
        document = self.documents[item]
        if isinstance(document, Document):
            document = document.unpack()
        elif isinstance(document, Encodable):
            document = document.x
        return self.transform(document)


@dc.dataclass
class TorchTrainerConfiguration(_TrainingConfiguration):
    objective: t.Optional[t.Union[Artifact, t.Callable]] = None
    loader_kwargs: t.Dict = dc.field(default_factory=dict)
    max_iterations: int = 10**100
    no_improve_then_stop: int = 5
    splitter: t.Optional[Artifact] = None
    download: bool = False
    validation_interval: int = 100
    watch: str = 'objective'
    optimizer_cls: Artifact = Artifact(torch.optim.Adam, serializer='pickle')
    optimizer_kwargs: t.Dict = dc.field(default_factory=dict)
    target_preprocessors: t.Optional[t.Union[Artifact, t.Dict]] = None
    compute_metrics: t.Optional[t.Union[Artifact, t.Callable]] = None

    def __post_init__(self):
        if self.compute_metrics and not isinstance(self.compute_metrics, Artifact):
            self.compute_metrics = Artifact(artifact=self.compute_metrics)

        if self.objective and not isinstance(self.objective, Artifact):
            self.objective = Artifact(artifact=self.objective)

        if self.target_preprocessors and not isinstance(
            self.target_preprocessors, Artifact
        ):
            self.target_preprocessors = Artifact(artifact=self.target_preprocessors)


@dc.dataclass
class Base:
    collate_fn: t.Optional[t.Union[Artifact, t.Callable]] = None
    is_batch: t.Optional[t.Union[Artifact, t.Callable]] = None
    num_directions: int = 2
    metrics: t.Optional[t.Sequence[t.Union[str, Metric]]] = None
    training_select: t.Optional[Select] = None

    @contextmanager
    def evaluating(self):
        raise NotImplementedError

    def train(self):
        raise NotImplementedError

    def stopping_criterion(self, iteration):
        max_iterations = self.training_configuration.max_iterations
        no_improve_then_stop = self.training_configuration.no_improve_then_stop
        if isinstance(max_iterations, int) and iteration >= max_iterations:
            return True
        if isinstance(no_improve_then_stop, int):
            if self.training_configuration.watch == 'objective':
                to_watch = [-x for x in self.metric_values['objective']]
            else:
                to_watch = self.metric_values[self.training_configuration.watch]

            if max(to_watch[-no_improve_then_stop:]) < max(to_watch):
                logging.info('early stopping triggered!')
                return True
        return False

    def saving_criterion(self):
        if self.training_configuration.watch == 'objective':
            to_watch = [-x for x in self.metric_values['objective']]
        else:
            to_watch = self.metric_values[self.training_configuration.watch]
        if all([to_watch[-1] >= x for x in to_watch[:-1]]):
            return True
        return False

    def _fit(
        self,
        X: t.Union[t.List[str], str],
        y: t.Optional[t.Union[t.List, t.Any]] = None,
        db: t.Optional[Datalayer] = None,
        select: t.Optional[t.Union[Select, t.Dict]] = None,
        configuration: t.Optional[TorchTrainerConfiguration] = None,
        validation_sets: t.Optional[t.List[str]] = None,
        metrics: t.Optional[t.List[Metric]] = None,
        data_prefetch: bool = False,
    ):
        if configuration is not None:
            self.training_configuration = configuration
        if select is not None:
            if isinstance(select, dict):
                select = Serializable.deserialize(select)
            self.training_select = select  # type: ignore[assignment]
        if validation_sets is not None:
            self.validation_sets = validation_sets
        if metrics is not None:
            self.metrics = metrics

        self.train_X = X
        self.train_y = y

        train_data, valid_data = self._get_data(db=db)

        loader_kwargs = self.training_configuration.loader_kwargs
        train_dataloader = DataLoader(train_data, **loader_kwargs)
        valid_dataloader = DataLoader(valid_data, **loader_kwargs)

        if db is None:
            raise ValueError('db cannot be None')
        return self._fit_with_dataloaders(
            train_dataloader,
            valid_dataloader,
            db=db,
            validation_sets=validation_sets or [],
        )

    def preprocess(self, r):
        raise NotImplementedError  # implemented in PyTorch wrapper and PyTorch pipeline

    def log(self, **kwargs):
        out = ''
        for k, v in kwargs.items():
            if isinstance(v, dict):
                for kk, vv in v.items():
                    out += f'{k}/{kk}: {vv}; '
            else:
                out += f'{k}: {v}; '
        logging.info(out)

    def forward(self, X):
        return self.object.artifact(X)

    def extract_batch_key(self, batch, key: t.Union[t.List[str], str]):
        if isinstance(key, str):
            return batch[key]
        return [batch[k] for k in key]

    def extract_batch(self, batch):
        if self.train_y is not None:
            return [
                self.extract_batch_key(batch, self.train_X),
                self.extract_batch_key(batch, self.train_y),
            ]
        return [self.extract_batch_key(batch, self.train_X)]

    def take_step(self, batch, optimizers):
        batch = self.extract_batch(batch)
        outputs = self.train_forward(*batch)
        objective_value = self.training_configuration.objective.artifact(*outputs)
        for opt in optimizers:
            opt.zero_grad()
        objective_value.backward()
        for opt in optimizers:
            opt.step()
        return objective_value

    def compute_validation_objective(self, valid_dataloader):
        objective_values = []
        with self.evaluating(), torch.no_grad():
            for batch in valid_dataloader:
                batch = self.extract_batch(batch)
                objective_values.append(
                    self.training_configuration.objective.artifact(
                        *self.train_forward(*batch)
                    ).item()
                )
            return sum(objective_values) / len(objective_values)

    def compute_metrics(self, validation_set, database):
        if validation_set not in self._validation_set_cache:
            self._validation_set_cache[validation_set] = database.load(
                'dataset', validation_set
            )
        data = [r.unpack() for r in self._validation_set_cache[validation_set].data]
        return self.training_configuration.compute_metrics.artifact(
            data,
            metrics=self.metrics,
            model=self,
        )

    def _fit_with_dataloaders(
        self,
        train_dataloader: DataLoader,
        valid_dataloader: DataLoader,
        db: Datalayer,
        validation_sets: t.List[str],
    ):
        self.train()
        iteration = 0
        while True:
            for batch in train_dataloader:
                train_objective = self.take_step(
                    batch, self.optimizers  # type: ignore[attr-defined]
                )
                self.log(fold='TRAIN', iteration=iteration, objective=train_objective)

                if iteration % self.training_configuration.validation_interval == 0:
                    valid_loss = self.compute_validation_objective(valid_dataloader)
                    all_metrics = {}
                    for vs in validation_sets:
                        metrics = self.compute_metrics(vs, db)
                        metrics = {f'{vs}/{k}': metrics[k] for k in metrics}
                        all_metrics.update(metrics)
                    all_metrics.update({'objective': valid_loss})
                    self.append_metrics(all_metrics)  # type: ignore[attr-defined]
                    self.log(fold='VALID', iteration=iteration, **all_metrics)
                    if self.saving_criterion():
                        self.save(db)  # type: ignore[attr-defined]
                    stop = self.stopping_criterion(iteration)
                    if stop:
                        return
                iteration += 1

    def train_preprocess(self):
        preprocessors = {}
        if isinstance(self.train_X, str):
            preprocessors[self.train_X] = (
                self.preprocess if self.preprocess else lambda x: x
            )
        else:
            for model, X in zip(self.models, self.train_X):
                preprocessors[X] = (
                    model.preprocess if model.preprocess is not None else lambda x: x
                )
        if self.train_y is not None:
            if (
                isinstance(self.train_y, str)
                and self.training_configuration.target_preprocessors
            ):
                preprocessors[
                    self.train_y
                ] = self.training_configuration.target_preprocessors.get(
                    self.train_y, lambda x: x
                )
            elif isinstance(self.train_y, str):
                preprocessors[self.train_y] = lambda x: x
            elif (
                isinstance(self.train_y, list)
                and self.training_configuration.target_preprocessors
            ):
                for y in self.train_y:
                    preprocessors[
                        y
                    ] = self.training_configuration.target_preprocessors.get(
                        y, lambda x: x
                    )

        for k in preprocessors:
            if isinstance(preprocessors[k], Artifact):
                preprocessors[k] = preprocessors[k].artifact
        return lambda r: {k: preprocessors[k](r[k]) for k in preprocessors}

    def _get_data(self, db: t.Optional[Datalayer]):
        if self.training_select is None:
            raise ValueError('self.training_select cannot be None')
        train_data = QueryDataset(
            select=self.training_select,
            keys=self.training_keys,  # type: ignore[attr-defined]
            fold='train',
            transform=self.train_preprocess(),
            db=db,
        )
        valid_data = QueryDataset(
            select=self.training_select,
            keys=self.training_keys,  # type: ignore[attr-defined]
            fold='valid',
            transform=self.train_preprocess(),
            db=db,
        )
        return train_data, valid_data


@dc.dataclass
class TorchModel(Base, Model):  # type: ignore[misc]
    optimizer_state: t.Optional[Artifact] = None
    forward_method: str = '__call__'
    train_forward_method: str = '__call__'

    """
    :param optimizer_state: optional optimizer state, populated automatically on reload
    :param forward_method: method to call for prediction, defaults to __call__
    :param train_forward_method: method to call for training, defaults to __call__
    """

    def __post_init__(self):
        super().__post_init__()

        self.object.serializer = 'torch'

        if self.optimizer_state is not None:
            self.optimizer.load_state_dict(self.optimizer_state.artifact)

        self._validation_set_cache = {}

    @cached_property
    def optimizer(self):
        return self.training_configuration.optimizer_cls.artifact(
            self.object.artifact.parameters(),
            **self.training_configuration.optimizer_kwargs,
        )

    @property
    def optimizers(self):
        return [self.optimizer]

    def save(self, database: Datalayer):
        self.optimizer_state = Artifact(self.optimizer.state_dict(), serializer='torch')
        database.replace(
            object=self,
            upsert=True,
        )

    @contextmanager
    def evaluating(self):
        yield eval(self)

    def train(self):
        return self.object.artifact.train()

    def eval(self):
        return self.object.artifact.eval()

    def parameters(self):
        return self.object.parameters()

    def state_dict(self):
        return self.object.state_dict()

    @contextmanager
    def saving(self):
        with super().saving():
            was_training = self.object.training
            try:
                self.object.eval()
                yield
            finally:
                if was_training:
                    self.object.train()

    def __getstate__(self):
        state = self.__dict__.copy()
        if isinstance(self.object, torch.jit.ScriptModule) or isinstance(
            self.object, torch.jit.ScriptFunction
        ):
            f = io.BytesIO()
            torch.jit.save(self.object, f)
            state['_object_bytes'] = f.getvalue()
        return state

    def __setstate__(self, state):
        keys = state.keys()
        for k in keys:
            if k != '_object_bytes':
                self.__dict__[k] = state[k]
            else:
                state.__dict__['object'] = torch.jit.load(
                    io.BytesIO(state.pop('object_bytes'))
                )

    def _predict_one(self, x):
        with torch.no_grad(), eval(self.object.artifact):
            if self.preprocess is not None:
                x = self.preprocess.artifact(x)
            x = to_device(x, device_of(self.object.artifact))
            singleton_batch = create_batch(x)
            method = getattr(self.object.artifact, self.forward_method)
            output = method(singleton_batch)
            output = to_device(output, 'cpu')
            args = unpack_batch(output)[0]
            if self.preprocess is not None:
                args = self.postprocess.artifact(args)
            return args

    def _predict(self, x, one: bool = False, **kwargs):  # type: ignore[override]
        with torch.no_grad(), eval(self.object.artifact):
            if one:
                return self._predict_one(x)
            inputs = BasicDataset(
                x,
                self.preprocess.artifact  # type: ignore[attr-defined]
                if self.preprocess is not None
                else lambda x: x,
            )
            loader = torch.utils.data.DataLoader(inputs, **kwargs)
            out = []
            for batch in tqdm(loader, total=len(loader)):
                batch = to_device(batch, device_of(self.object.artifact))

                method = getattr(self.object.artifact, self.forward_method)
                tmp = method(batch)
                tmp = to_device(tmp, 'cpu')
                tmp = unpack_batch(tmp)
                if self.postprocess is not None:
                    tmp = [
                        self.postprocess.artifact(t)  # type: ignore[union-attr]
                        for t in tmp
                    ]
                out.extend(tmp)
            return out

    def train_forward(self, X, y=None):
        method = getattr(self.object.artifact, self.train_forward_method)
        if hasattr(self.object.artifact, 'train_forward'):
            if y is None:
                return method(X)
            else:
                return method(X, y=y)
        else:
            if y is None:
                return (method(X),)
            else:
                return [method(X), y]


def unpack_batch(args):
    """
    Unpack a batch into lines of tensor output.

    :param args: a batch of model outputs

    >>> unpack_batch(torch.randn(1, 10))[0].shape
    torch.Size([10])
    >>> out = unpack_batch([torch.randn(2, 10), torch.randn(2, 3, 5)])
    >>> type(out)
    <class 'list'>
    >>> len(out)
    2
    >>> out = unpack_batch({'a': torch.randn(2, 10), 'b': torch.randn(2, 3, 5)})
    >>> [type(x) for x in out]
    [<class 'dict'>, <class 'dict'>]
    >>> out[0]['a'].shape
    torch.Size([10])
    >>> out[0]['b'].shape
    torch.Size([3, 5])
    >>> out = unpack_batch({'a': {'b': torch.randn(2, 10)}})
    >>> out[0]['a']['b'].shape
    torch.Size([10])
    >>> out[1]['a']['b'].shape
    torch.Size([10])
    """

    if isinstance(args, torch.Tensor):
        return [args[i] for i in range(args.shape[0])]
    else:
        if isinstance(args, list) or isinstance(args, tuple):
            tmp = [unpack_batch(x) for x in args]
            batch_size = len(tmp[0])
            return [[x[i] for x in tmp] for i in range(batch_size)]
        elif isinstance(args, dict):
            tmp = {k: unpack_batch(v) for k, v in args.items()}
            batch_size = len(next(iter(tmp.values())))
            return [{k: v[i] for k, v in tmp.items()} for i in range(batch_size)]
        else:  # pragma: no cover
            raise NotImplementedError


def create_batch(args):
    """
    Create a singleton batch in a manner similar to the PyTorch dataloader

    :param args: single data point for batching

    >>> create_batch(3.).shape
    torch.Size([1])
    >>> x, y = create_batch([torch.randn(5), torch.randn(3, 7)])
    >>> x.shape
    torch.Size([1, 5])
    >>> y.shape
    torch.Size([1, 3, 7])
    >>> d = create_batch(({'a': torch.randn(4)}))
    >>> d['a'].shape
    torch.Size([1, 4])
    """
    if isinstance(args, (tuple, list)):
        return tuple([create_batch(x) for x in args])
    if isinstance(args, dict):
        return {k: create_batch(args[k]) for k in args}
    if isinstance(args, torch.Tensor):
        return args.unsqueeze(0)
    if isinstance(args, (float, int)):
        return torch.tensor([args])
    raise TypeError(
        'only tensors and tuples of tensors recursively supported...'
    )  # pragma: no cover