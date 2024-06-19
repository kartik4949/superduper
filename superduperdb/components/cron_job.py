import typing as t

from superduperdb import CFG, Component, logging
from superduperdb.base.datalayer import Datalayer
from superduperdb.components.component import ensure_initialized
from superduperdb.components.datatype import dill_serializer
from superduperdb.misc.server import request_server


class CronJob(Component):
    """Run a job on a schedule.

    ***Note that this feature deploys on SuperDuperDB Enterprise.***

    :param schedule: Schedule in cron format.
    """

    type_id: t.ClassVar[str] = 'cronjob'
    schedule: str = '0 0 * * *'

    def post_create(self, db: Datalayer) -> None:
        """
        Post-create hook.

        :param db: Datalayer instance.
        """
        super().post_create(db)
        if CFG.cluster.crontab.uri is not None:
            request_server(
                service='crontab',
                endpoint='add',
                args={'identifier': self.identifier},
                type='post',
            )
        else:
            logging.warn('No crontab service found - cron-job will not schedule')

    @ensure_initialized
    def run(self):
        """Run the job."""
        raise NotImplementedError


class FunctionCronJob(CronJob):
    """
    Run a function on a schedule.

    :param function: Callable to run
    """

    _artifacts = (('function', dill_serializer),)

    function: t.Callable

    @ensure_initialized
    def run(self):
        """Run the function."""
        self.function(self.db)
