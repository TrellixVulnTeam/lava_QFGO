import datetime
import json
import logging

from django.core.files.base import ContentFile
from django.db import connection
from django.db import IntegrityError, transaction
from django.db.models import Q
from django.db.utils import DatabaseError

from twisted.internet.threads import deferToThread

from zope.interface import implements

from lava_scheduler_app.models import Device, TestJob
from lava_scheduler_daemon.jobsource import IJobSource

try:
    from psycopg2 import InterfaceError, OperationalError
except ImportError:
    class InterfaceError(Exception):
        pass
    class OperationalError(Exception):
        pass


class DatabaseJobSource(object):

    implements(IJobSource)

    logger = logging.getLogger(__name__ + '.DatabaseJobSource')

    def deferForDB(self, func, *args, **kw):
        def wrapper(*args, **kw):
            # If there is no db connection yet on this thread, create a
            # connection and immediately commit, because rolling back the
            # first transaction on a connection loses the effect of
            # settings.TIME_ZONE when using postgres (see
            # https://code.djangoproject.com/ticket/17062).
            transaction.enter_transaction_management()
            try:
                if connection.connection is None:
                    connection.cursor().close()
                    assert connection.connection is not None
                    transaction.commit()
                try:
                    return func(*args, **kw)
                except (DatabaseError, OperationalError, InterfaceError), error:
                    message = str(error)
                    if message == 'connection already closed' or \
                       message.startswith(
                        'terminating connection due to administrator command') or \
                       message.startswith(
                        'could not connect to server: Connection refused'):
                        self.logger.warning(
                            'Forcing reconnection on next db access attempt')
                        if connection.connection:
                            if not connection.connection.closed:
                                connection.connection.close()
                            connection.connection = None
                    raise
            finally:
                # In Django 1.2, the commit_manually() etc decorators only
                # commit or rollback the transaction if Django thinks there's
                # been a write to the database.  We don't want to leave
                # transactions dangling under any circumstances so we
                # unconditionally issue a rollback.  This might be a teensy
                # bit wastful, but it wastes a lot less time than figuring out
                # why your south migration appears to have got stuck...
                transaction.rollback()
                transaction.leave_transaction_management()
        return deferToThread(wrapper, *args, **kw)

    def getBoardList_impl(self):
        return [d.hostname for d in Device.objects.all()]

    def getBoardList(self):
        return self.deferForDB(self.getBoardList_impl)

    def getJobForBoard_impl(self, board_name):
        while True:
            device = Device.objects.get(hostname=board_name)
            if device.status != Device.IDLE:
                return None
            jobs_for_device = TestJob.objects.all().filter(
                Q(requested_device=device)
                | Q(requested_device_type=device.device_type),
                status=TestJob.SUBMITTED)
            jobs_for_device = jobs_for_device.extra(
                select={'is_targeted': 'requested_device_id is not NULL'},
                order_by=['-is_targeted', 'submit_time'])
            jobs = jobs_for_device[:1]
            if jobs:
                job = jobs[0]
                job.status = TestJob.RUNNING
                job.start_time = datetime.datetime.utcnow()
                job.actual_device = device
                device.status = Device.RUNNING
                device.current_job = job
                try:
                    # The unique constraint on current_job may cause this to
                    # fail in the case of concurrent requests for different
                    # boards grabbing the same job.  If there are concurrent
                    # requests for the *same* board they may both return the
                    # same job -- this is an application level bug though.
                    device.save()
                except IntegrityError:
                    self.logger.info(
                        "job %s has been assigned to another board -- "
                        "rolling back", job.id)
                    transaction.rollback()
                    continue
                else:
                    job.log_file.save(
                        'job-%s.log' % job.id, ContentFile(''), save=False)
                    job.save()
                    json_data = json.loads(job.definition)
                    json_data['target'] = device.hostname
                    transaction.commit()
                    return json_data
            else:
                return None

    def getJobForBoard(self, board_name):
        return self.deferForDB(self.getJobForBoard_impl, board_name)

    def getLogFileForJobOnBoard_impl(self, board_name):
        device = Device.objects.get(hostname=board_name)
        job = device.current_job
        log_file = job.log_file
        log_file.file.close()
        log_file.open('wb')
        return log_file

    def getLogFileForJobOnBoard(self, board_name):
        return self.deferForDB(self.getLogFileForJobOnBoard_impl, board_name)

    def jobCompleted_impl(self, board_name, exit_code):
        self.logger.debug('marking job as complete on %s', board_name)
        device = Device.objects.get(hostname=board_name)
        if device.status == Device.RUNNING:
            device.status = Device.IDLE
        elif device.status == Device.OFFLINING:
            device.status = Device.OFFLINE
        else:
            self.logger.error(
                "Unexpected device state in jobCompleted: %s" % device.status)
            device.status = Device.IDLE
        job = device.current_job
        device.current_job = None
        if job.status == TestJob.RUNNING:
            if exit_code == 0:
                job.status = TestJob.COMPLETE
            else:
                job.status = TestJob.INCOMPLETE
        elif job.status == TestJob.CANCELING:
            job.status = TestJob.CANCELED
        else:
            self.logger.error(
                "Unexpected job state in jobCompleted: %s" % job.status)
            job.status = TestJob.COMPLETE
        job.end_time = datetime.datetime.utcnow()
        device.save()
        job.save()

    def jobCompleted(self, board_name, exit_code):
        return self.deferForDB(self.jobCompleted_impl, board_name, exit_code)

    def jobOobData_impl(self, board_name, key, value):
        self.logger.info(
            "oob data received for %s: %s: %s", board_name, key, value)
        if key == 'dashboard-put-result':
            device = Device.objects.get(hostname=board_name)
            device.current_job.results_link = value
            device.current_job.save()
            transaction.commit()

    def jobOobData(self, board_name, key, value):
        return self.deferForDB(self.jobOobData_impl, board_name, key, value)

    def jobCheckForCancellation_impl(self, board_name):
        device = Device.objects.get(hostname=board_name)
        job = device.current_job
        return job.status != TestJob.RUNNING

    def jobCheckForCancellation(self, board_name):
        return self.deferForDB(self.jobCheckForCancellation_impl, board_name)
