#
# Copyright 2017 the original author or authors.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
from task import Task
from twisted.internet.defer import inlineCallbacks, TimeoutError, failure
from twisted.internet import reactor


class MibUploadTask(Task):
    """
    OpenOMCI MIB upload task

    On successful completion, this task will call the 'callback' method of the
    deferred returned by the start method. Only a textual message is provided as
    the successful results and it lists the number of ME entities successfully
    retrieved.

    Note that the MIB Synchronization State Machine will get event subscription
    information for the MIB Reset and MIB Upload Next requests and it is the
    MIB Synchronization State Machine that actually populates the MIB Database.
    """
    task_priority = 250
    name = "MIB Upload Task"

    def __init__(self, omci_agent, device_id):
        """
        Class initialization

        :param omci_agent: (OmciAdapterAgent) OMCI Adapter agent
        :param device_id: (str) ONU Device ID
        """
        super(MibUploadTask, self).__init__(MibUploadTask.name,
                                            omci_agent,
                                            device_id,
                                            priority=MibUploadTask.task_priority)
        self._local_deferred = None

    def cancel_deferred(self):
        super(MibUploadTask, self).cancel_deferred()

        d, self._local_deferred = self._local_deferred, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass

    def start(self):
        """
        Start MIB Synchronization tasks
        """
        super(MibUploadTask, self).start()
        self._local_deferred = reactor.callLater(0, self.perform_mib_upload)

    def stop(self):
        """
        Shutdown MIB Synchronization tasks
        """
        self.log.debug('stopping')

        self.cancel_deferred()
        super(MibUploadTask, self).stop()

    @inlineCallbacks
    def perform_mib_upload(self):
        """
        Perform the MIB Upload sequence
        """
        self.log.info('perform-mib-upload')

        seq_no = 0
        number_of_commands = 0

        try:
            device = self.omci_agent.get_device(self.device_id)

            #########################################
            # MIB Reset
            yield device.omci_cc.send_mib_reset()

            ########################################
            # Begin MIB Upload
            results = yield device.omci_cc.send_mib_upload()
            number_of_commands = results.fields['omci_message'].fields['number_of_commands']

            for seq_no in xrange(number_of_commands):
                if not device.active or not device.omci_cc.enabled:
                    self.deferred.errback(failure.Failure(
                        GeneratorExit('OMCI and/or ONU is not active')))
                    return
                yield device.omci_cc.send_mib_upload_next(seq_no)

            # Successful if here
            self.log.info('mib-synchronized')
            self.deferred.callback('success, loaded {} ME Instances'.
                                   format(number_of_commands))

        except TimeoutError as e:
            self.log.warn('mib-upload-timeout', e=e, seq_no=seq_no,
                          number_of_commands=number_of_commands)
            self.deferred.errback(failure.Failure(e))

        except Exception as e:
            self.log.exception('mib-upload', e=e)
            self.deferred.errback(failure.Failure(e))

