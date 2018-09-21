#
# Copyright 2018 the original author or authors.
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
from twisted.internet import reactor
from twisted.internet.defer import inlineCallbacks, returnValue, TimeoutError, failure
from voltha.extensions.omci.omci_me import *
from voltha.extensions.omci.tasks.task import Task
from voltha.extensions.omci.omci_defs import *

OP = EntityOperations
RC = ReasonCodes


class ServiceDownloadFailure(Exception):
    """
    This error is raised by default when the download fails
    """


class ServiceResourcesFailure(Exception):
    """
    This error is raised by when one or more resources required is not available
    """


class AdtnServiceDownloadTask(Task):
    """
    OpenOMCI MIB Download Example - Service specific

    This task takes the legacy OMCI 'script' for provisioning the Adtran ONU
    and converts it to run as a Task on the OpenOMCI Task runner.  This is
    in order to begin to decompose service instantiation in preparation for
    Technology Profile work.

    Once technology profiles are ready, some of this task may hang around or
    be moved into OpenOMCI if there are any very common settings/configs to do
    for any profile that may be provided in the v2.0 release

    Currently, the only service tech profiles expected by v2.0 will be for AT&T
    residential data service and DT residential data service.
    """
    task_priority = Task.DEFAULT_PRIORITY + 10
    default_tpid = 0x8100
    default_gem_payload = 1518
    BRDCM_DEFAULT_VLAN = 4091

    name = "ADTRAN MIB Download Example Task"

    def __init__(self, omci_agent, handler):
        """
        Class initialization

        :param omci_agent: (OmciAdapterAgent) OMCI Adapter agent
        :param device_id: (str) ONU Device ID
        """
        super(AdtnServiceDownloadTask, self).__init__(AdtnServiceDownloadTask.name,
                                                      omci_agent,
                                                      handler.device_id,
                                                      priority=AdtnServiceDownloadTask.task_priority)
        self._handler = handler
        self._onu_device = omci_agent.get_device(handler.device_id)
        self._local_deferred = None

        self._vlan_tcis_1 = 0x900
        self._input_tpid = AdtnServiceDownloadTask.default_tpid
        self._output_tpid = AdtnServiceDownloadTask.default_tpid

        if self._handler.xpon_support:
            device = self._handler.adapter_agent.get_device(self.device_id)
            self._cvid = device.vlan
        else:
            # TODO: TCIS below is just a test, may need 0x900...as in the xPON mode
            self._vlan_tcis_1 = AdtnServiceDownloadTask.BRDCM_DEFAULT_VLAN
            self._cvid = AdtnServiceDownloadTask.BRDCM_DEFAULT_VLAN

        # Entity IDs. IDs with values can probably be most anything for most ONUs,
        #             IDs set to None are discovered/set
        #
        # TODO: Probably need to store many of these in the appropriate object (UNI, PON,...)
        #
        self._ieee_mapper_service_profile_entity_id = 0x100
        self._gal_enet_profile_entity_id = 0x100

        # Next to are specific
        self._ethernet_uni_entity_id = self._handler.uni_ports[0].entity_id
        self._vlan_config_entity_id = self._vlan_tcis_1

    def cancel_deferred(self):
        super(AdtnServiceDownloadTask, self).cancel_deferred()

        d, self._local_deferred = self._local_deferred, None
        try:
            if d is not None and not d.called:
                d.cancel()
        except:
            pass

    def start(self):
        """
        Start the MIB Service Download
        """
        super(AdtnServiceDownloadTask, self).start()
        self._local_deferred = reactor.callLater(0, self.perform_service_download)

    def stop(self):
        """
        Shutdown MIB Service download
        """
        self.log.debug('stopping')

        self.cancel_deferred()
        super(AdtnServiceDownloadTask, self).stop()

    def check_status_and_state(self, results, operation=''):
        """
        Check the results of an OMCI response.  An exception is thrown
        if the task was cancelled or an error was detected.

        :param results: (OmciFrame) OMCI Response frame
        :param operation: (str) what operation was being performed
        :return: True if successful, False if the entity existed (already created)
        """
        omci_msg = results.fields['omci_message'].fields
        status = omci_msg['success_code']
        error_mask = omci_msg.get('parameter_error_attributes_mask', 'n/a')
        failed_mask = omci_msg.get('failed_attributes_mask', 'n/a')
        unsupported_mask = omci_msg.get('unsupported_attributes_mask', 'n/a')

        self.log.debug(operation, status=status, error_mask=error_mask,
                       failed_mask=failed_mask, unsupported_mask=unsupported_mask)

        if status == RC.Success:
            self.strobe_watchdog()
            return True

        elif status == RC.InstanceExists:
            return False

        raise ServiceDownloadFailure('{} failed with a status of {}, error_mask: {}, failed_mask: {}, unsupported_mask: {}'
                                     .format(operation, status, error_mask, failed_mask, unsupported_mask))

    @inlineCallbacks
    def perform_service_download(self):
        """
        Send the commands to minimally configure the PON, Bridge, and
        UNI ports for this device. The application of any service flows
        and other characteristics are done once resources (gem-ports, tconts, ...)
        have been defined.
        """
        self.log.info('perform-service-download')

        device = self._handler.adapter_agent.get_device(self.device_id)

        def resources_available():
            # TODO: Rework for non-xpon mode
            if self._handler.xpon_support:
                return (device.vlan > 0 and
                        len(self._handler.uni_ports) > 0 and
                        len(self._handler.pon_port.tconts) and
                        len(self._handler.pon_port.gem_ports))
            else:
                return (len(self._handler.uni_ports) > 0 and
                        len(self._handler.pon_port.tconts) and
                        len(self._handler.pon_port.gem_ports))

        if self._handler.enabled and resources_available():
            device.reason = 'Performing Service OMCI Download'
            self._handler.adapter_agent.update_device(device)

            try:
                # Lock the UNI ports to prevent any alarms during initial configuration
                # of the ONU
                self.strobe_watchdog()
                # Provision the initial bridge configuration
                yield self.perform_service_specific_steps()

                # And re-enable the UNIs if needed
                yield self.enable_unis(self._handler.uni_ports, False)

                # If here, we are done
                device = self._handler.adapter_agent.get_device(self.device_id)

                device.reason = ''
                self._handler.adapter_agent.update_device(device)
                self.deferred.callback('service-download-success')

            except TimeoutError as e:
                self.deferred.errback(failure.Failure(e))

        else:
            # TODO: Provide better error reason, what was missing...
            e = ServiceResourcesFailure('Required resources are not available')
            self.deferred.errback(failure.Failure(e))

    @inlineCallbacks
    def perform_service_specific_steps(self):
        omci_cc = self._onu_device.omci_cc
        frame = None

        try:
            ################################################################################
            # TCONTS
            #
            #  EntityID will be referenced by:
            #            - GemPortNetworkCtp
            #  References:
            #            - ONU created TCONT (created on ONU startup)

            tcont_idents = self._onu_device.query_mib(Tcont.class_id)
            self.log.debug('tcont-idents', tcont_idents=tcont_idents)

            for tcont in self._handler.pon_port.tconts.itervalues():
                free_entity_id = next((k for k, v in tcont_idents.items()
                                       if isinstance(k, int) and
                                       v.get('attributes', {}).get('alloc_id', 0) == 0xFFFF), None)
                if free_entity_id is None:
                    self.log.error('no-available-tconts')
                    break
                # TODO: Need to restore on failure.  Need to check status/results
                yield tcont.add_to_hardware(omci_cc, free_entity_id)

            ################################################################################
            # GEMS  (GemPortNetworkCtp and GemInterworkingTp)
            #
            #  For both of these MEs, the entity_id is the GEM Port ID. The entity id of the
            #  GemInterworkingTp ME could be different since it has an attribute to specify
            #  the GemPortNetworkCtp entity id.
            #
            #  TODO: In the GEM Port routine to add, it has a hardcoded upstream TM ID of 0x8000
            #        for the GemPortNetworkCtp ME
            #
            #  GemPortNetworkCtp
            #    EntityID will be referenced by:
            #              - GemInterworkingTp
            #    References:
            #              - TCONT
            #              - Hardcoded upstream TM Entity ID
            #              - (Possibly in Future) Upstream Traffic descriptor profile pointer
            #
            #  GemInterworkingTp
            #    EntityID will be referenced by:
            #              - Ieee8021pMapperServiceProfile
            #    References:
            #              - GemPortNetworkCtp
            #              - Ieee8021pMapperServiceProfile
            #              - GalEthernetProfile
            #

            for gem_port in self._handler.pon_port.gem_ports.itervalues():
                tcont = gem_port.tcont
                if tcont is None:
                    self.log.error('unknown-tcont-reference', gem_id=gem_port.gem_id)
                    continue
                # TODO: Need to restore on failure.  Need to check status/results
                yield gem_port.add_to_hardware(omci_cc,
                                               tcont.entity_id,
                                               self._ieee_mapper_service_profile_entity_id,
                                               self._gal_enet_profile_entity_id)

            ################################################################################
            # Update the IEEE 802.1p Mapper Service Profile config
            #
            #  EntityID was created prior to this call. This is a set
            #
            #  References:
            #            - Gem Interworking TPs are set here
            #
            # TODO: All p-bits currently go to the one and only GEMPORT ID for now
            gem_ports = self._handler.pon_port.gem_ports
            gem_entity_ids = [gem_port.entity_id for _, gem_port in gem_ports.items()] \
                if len(gem_ports) else [OmciNullPointer]

            frame = Ieee8021pMapperServiceProfileFrame(
                self._ieee_mapper_service_profile_entity_id,  # 802.1p mapper Service Mapper Profile ID
                interwork_tp_pointers=gem_entity_ids          # Interworking TP IDs
            ).set()
            results = yield omci_cc.send(frame)
            self.check_status_and_state(results, 'set-8021p-mapper-service-profile')

            ################################################################################
            # Create Extended VLAN Tagging Operation config (PON-side)
            #
            #  EntityID relates to the VLAN TCIS
            #  References:
            #            - VLAN TCIS from previously created VLAN Tagging filter data
            #            - PPTP Ethernet UNI
            #
            # TODO: add entry here for additional UNI interfaces

            attributes = dict(
                association_type=2,                                 # Assoc Type, PPTP Ethernet UNI
                associated_me_pointer=self._ethernet_uni_entity_id  # Assoc ME, PPTP Entity Id
            )

            frame = ExtendedVlanTaggingOperationConfigurationDataFrame(
                self._vlan_config_entity_id,
                attributes=attributes
            ).create()
            results = yield omci_cc.send(frame)
            self.check_status_and_state(results, 'create-extended-vlan-tagging-operation-configuration-data')

            ################################################################################
            # Update Extended VLAN Tagging Operation Config Data
            #
            # Specifies the TPIDs in use and that operations in the downstream direction are
            # inverse to the operations in the upstream direction
            # TODO: Downstream mode may need to be modified once we work more on the flow rules

            attributes = dict(
                input_tpid=self._input_tpid,    # input TPID
                output_tpid=self._output_tpid,  # output TPID
                downstream_mode=0,              # inverse of upstream
            )
            frame = ExtendedVlanTaggingOperationConfigurationDataFrame(
                self._vlan_config_entity_id,
                attributes=attributes
            ).set()
            results = yield omci_cc.send(frame)
            self.check_status_and_state(results, 'set-extended-vlan-tagging-operation-configuration-data')

            ################################################################################
            # Update Extended VLAN Tagging Operation Config Data
            #
            # parameters: Entity Id ( 0x900), Filter Inner Vlan Id(0x1000-4096,do not filter on Inner vid,
            #             Treatment Inner Vlan Id : 2

            attributes = dict(
                received_frame_vlan_tagging_operation_table=
                VlanTaggingOperation(
                    filter_outer_priority=15,  # This entry is not a double-tag rule
                    filter_outer_vid=4096,     # Do not filter on the outer VID value
                    filter_outer_tpid_de=0,    # Do not filter on the outer TPID field

                    filter_inner_priority=15,  # This is a no-tag rule, ignore all other VLAN tag filter fields
                    filter_inner_vid=0x1000,   # Do not filter on the inner VID
                    filter_inner_tpid_de=0,    # Do not filter on inner TPID field

                    filter_ether_type=0,         # Do not filter on EtherType
                    treatment_tags_to_remove=0,  # Remove 0 tags

                    treatment_outer_priority=15,  # Do not add an outer tag
                    treatment_outer_vid=0,        # n/a
                    treatment_outer_tpid_de=0,    # n/a

                    treatment_inner_priority=0,      # Add an inner tag and insert this value as the priority
                    treatment_inner_vid=self._cvid,  # use this value as the VID in the inner VLAN tag
                    treatment_inner_tpid_de=4,       # set TPID
                )
            )
            frame = ExtendedVlanTaggingOperationConfigurationDataFrame(
                self._vlan_config_entity_id,  # Entity ID
                attributes=attributes         # See above
            ).set()
            results = yield omci_cc.send(frame)
            self.check_status_and_state(results, 'set-extended-vlan-tagging-operation-configuration-data-untagged')

            ################################################################################
            ################################################################################
            ################################################################################
            # BP: This is for AT&T RG's                #
            #   TODO: CB: NOTE: TRY THIS ONCE OTHER SEQUENCES WORK
            #
            # Set AR - ExtendedVlanTaggingOperationConfigData
            #          514 - RxVlanTaggingOperationTable - add VLAN <cvid> to priority tagged pkts - c-vid
            # results = yield omci.send_set_extended_vlan_tagging_operation_vlan_configuration_data_single_tag(
            #                                 0x900,  # Entity ID
            #                                 8,      # Filter Inner Priority, do not filter on Inner Priority
            #                                 0,    # Filter Inner VID, this will be 0 in CORD
            #                                 0,      # Filter Inner TPID DE
            #                                 1,      # Treatment tags, number of tags to remove
            #                                 8,      # Treatment inner priority, copy Inner Priority
            #                                 2)   # Treatment inner VID, this will be 2 in CORD
            # Set AR - ExtendedVlanTaggingOperationConfigData
            #          514 - RxVlanTaggingOperationTable - add VLAN <cvid> to priority tagged pkts - c-vid
            # results = yield omci.send_set_extended_vlan_tagging_operation_vlan_configuration_data_single_tag(
            #                                 0x200,  # Entity ID
            #                                 8,      # Filter Inner Priority
            #                                 0,      # Filter Inner VID
            #                                 0,      # Filter Inner TPID DE
            #                                 1,      # Treatment tags to remove
            #                                 8,      # Treatment inner priority
            #                                 cvid)   # Treatment inner VID
            # Set AR - ExtendedVlanTaggingOperationConfigData
            #          514 - RxVlanTaggingOperationTable - add VLAN <cvid> to untagged pkts - c-vid
            # results = yield omci.send_set_extended_vlan_tagging_operation_vlan_configuration_data_untagged(
            #                                0x100,   # Entity ID            BP: Oldvalue 0x202
            #                                0x1000,  # Filter Inner VID     BP: Oldvalue 0x1000
            #                                cvid)    # Treatment inner VID  BP: cvid
            # success = results.fields['omci_message'].fields['success_code'] == 0
            # error_mask = results.fields['omci_message'].fields['parameter_error_attributes_mask']

            ###############################################################################

        except TimeoutError as e:
            self.log.warn('rx-timeout-2', frame=frame)
            raise

        except Exception as e:
            self.log.exception('omci-setup-2', e=e)
            raise

        returnValue(None)

    @inlineCallbacks
    def enable_unis(self, unis, force_lock):
        """
        Lock or unlock one or more UNI ports

        :param unis: (list) of UNI objects
        :param force_lock: (boolean) If True, force lock regardless of enabled state
        """
        omci_cc = self._onu_device.omci_cc
        frame = None

        for uni in unis:
            ################################################################################
            #  Lock/Unlock UNI  -  0 to Unlock, 1 to lock
            #
            #  EntityID is referenced by:
            #            - MAC bridge port configuration data for the UNI side
            #  References:
            #            - Nothing
            try:
                state = 1 if force_lock or not uni.enabled else 0
                frame = PptpEthernetUniFrame(uni.entity_id,
                                             attributes=dict(administrative_state=state)).set()
                results = yield omci_cc.send(frame)
                self.check_status_and_state(results, 'set-pptp-ethernet-uni-lock-restore')

            except TimeoutError:
                self.log.warn('rx-timeout', frame=frame)
                raise

            except Exception as e:
                self.log.exception('omci-failure', e=e)
                raise

        returnValue(None)








