from vertigo_middleware.common.utils import make_swift_request, \
    set_object_metadata, get_object_metadata
from vertigo_middleware.gateways.docker.runtime import RunTimeSandbox, \
    VertigoInvocationProtocol
from shutil import copy2
import os


MC_MAIN_HEADER = "X-Object-Meta-Microcontroller-Main"
MC_DEP_HEADER = "X-Object-Meta-Microcontroller-Library-Dependency"


class VertigoGatewayDocker():

    def __init__(self, request, response, conf, logger, account):
        self.request = request
        self.response = response
        self.conf = conf
        self.logger = logger
        self.account = account
        self.method = self.request.method.lower()
        self.scope = account[5:18]
        self.mc_timeout = conf["mc_timeout"]
        self.mc_container = conf["mc_container"]
        self.dep_container = conf["mc_dependency"]

        # Paths
        self.logger_path = os.path.join(conf["log_dir"], self.scope)
        self.pipe_path = os.path.join(conf["pipes_dir"], self.scope)
        self.mc_pipe_path = os.path.join(self.pipe_path, conf["mc_pipe"])

    def execute_microcontrollers(self, mc_list):
        """
        Exeutes the microcontroller list.
         1. Starts the docker container (sandbox).
         3. Gets the microcontrollers metadata.
         4. Executes the microcontroller list.

        :param mc_list: microcontroller list
        :returns: response from the microcontrollers
        """
        RunTimeSandbox(self.logger, self.conf, self.account).start()

        mc_metadata = self._get_microcontroller_metadata(mc_list)
        object_headers = self._get_object_headers()

        protocol = VertigoInvocationProtocol(self.mc_pipe_path,
                                             self.logger_path,
                                             dict(self.request.headers),
                                             object_headers,
                                             mc_list,
                                             mc_metadata,
                                             self.mc_timeout,
                                             self.logger)
        return protocol.communicate()

    def _get_object_headers(self):
        headers = dict()
        if self.method == "get":
            headers = self.response.headers
        elif self.method == "put":
            if 'Content-Length' in self.request.headers:
                headers['Content-Length'] = self.request.headers['Content-Length']
            if 'Content-Type' in self.request.headers:
                headers['Content-Type'] = self.request.headers['Content-Type']
            for header in self.request.headers:
                if header.startswith('X-Object'):
                    headers[header] = self.request.headers[header]

            referer = self.request.method+' '+self.request.host_url+self.request.path_info
            self.request.headers['Referer'] = referer  # Needed for the mc_engine

        return headers

    def _update_local_cache_from_swift(self, swift_container, object_name):
        """
        Updates the local cache of microcontrollers and dependencies

        :param container: container name
        :param object_name: Name of the microcontroller or dependency
        """
        cache_target_path = os.path.join(self.conf["cache_dir"],
                                         self.scope, 'vertigo',
                                         swift_container)
        cache_target_obj = os.path.join(cache_target_path, object_name)

        if not os.path.exists(cache_target_path):
            os.makedirs(cache_target_path, 0o777)

        resp = make_swift_request("GET", self.account, swift_container, object_name)

        with open(cache_target_obj, 'w') as fn:
            fn.write(resp.body)

        set_object_metadata(cache_target_obj, resp.headers)

    def _is_avialable_in_cache(self, swift_container, obj_name):
        """
        checks whether the microcontroler or the dependency is in cache. If not,
        brings it from swift.

        :param swift_container: container name (microcontroller or dependency)
        :param object_name: Name of the microcontroller or dependency
        :returns : whether the object is available in cache
        """
        cached_target_obj = os.path.join(self.conf["cache_dir"], self.scope,
                                         'vertigo', swift_container, obj_name)
        self.logger.info('Vertigo - Checking in cache: ' + swift_container +
                         '/' + obj_name)

        if not os.path.isfile(cached_target_obj):
            # If the objects is not in cache, brings it from Swift.
            # TODO(josep): In normal usage, if the object is not in cache, the
            # request fails. The idea is that the cache will be automatically
            # updated by another service.
            # raise NameError('Vertigo - ' + swift_container+'/'+object_name +
            #                 ' not found in cache.')
            self.logger.info('Vertigo - ' + swift_container +
                             '/' + obj_name + ' not found in cache.')
            self._update_local_cache_from_swift(swift_container, obj_name)
        else:
            self._update_local_cache_from_swift(swift_container, obj_name)  # DELETE! (Only for test purposes)
            self.logger.info('Vertigo - ' + swift_container +
                             '/' + obj_name + ' in cache.')

        return True

    def _update_from_cache(self, mc_main, swift_container, obj_name):
        """
        Updates the tenant microcontroller folder from the local cache.

        :param mc_main: main class of the microcontroller
        :param swift_container: container name (microcontroller or dependency)
        :param object_name: Name of the microcontroller or dependency
        """
        # if enter to this method means that the objects exist in cache
        cached_target_obj = os.path.join(self.conf["cache_dir"], self.scope,
                                         'vertigo', swift_container, obj_name)
        docker_target_dir = os.path.join(
            self.conf["mc_dir"], self.scope, mc_main)
        docker_target_obj = os.path.join(docker_target_dir, obj_name)
        update_from_cache = False

        if not os.path.exists(docker_target_dir):
            os.makedirs(docker_target_dir, 0o777)
            update_from_cache = True
        elif not os.path.isfile(docker_target_obj):
            update_from_cache = True
        else:
            cached_obj_metadata = get_object_metadata(cached_target_obj)
            docker_obj_metadata = get_object_metadata(docker_target_obj)

            cached_obj_tstamp = float(cached_obj_metadata['X-Timestamp'])
            docker_obj_tstamp = float(docker_obj_metadata['X-Timestamp'])

            if cached_obj_tstamp > docker_obj_tstamp:
                update_from_cache = True

        if update_from_cache:
            self.logger.info('Vertigo - Going to update from cache: ' +
                             swift_container + '/' + obj_name)
            copy2(cached_target_obj, docker_target_obj)
            metadata = get_object_metadata(cached_target_obj)
            set_object_metadata(docker_target_obj, metadata)

    def _get_metadata(self, swift_container, obj_name):
        """
        Retrieves the swift metadata from the local cached object.

        :param swift_container: container name (microcontroller or dependency)
        :param object_name: object name
        :returns: swift metadata dictionary
        """
        cached_target_obj = os.path.join(self.conf["cache_dir"], self.scope,
                                         'vertigo', swift_container, obj_name)
        metadata = get_object_metadata(cached_target_obj)

        return metadata

    def _get_microcontroller_metadata(self, mc_list):
        """
        Retrieves the microcontroller metadata from the list of
        microcontrollers.

        :param mc_list: microcontroller list
        :returns: metadata dictionary
        """
        mc_metadata = dict()

        for mc_name in mc_list:
            if self._is_avialable_in_cache(self.mc_container, mc_name):
                mc_metadata[mc_name] = self._get_metadata(self.mc_container,
                                                          mc_name)
                mc_main = mc_metadata[mc_name][MC_MAIN_HEADER]
                self._update_from_cache(mc_main, self.mc_container, mc_name)

                if mc_metadata[mc_name][MC_DEP_HEADER]:
                    dep_list = mc_metadata[mc_name][MC_DEP_HEADER].split(",")
                    for dep_name in dep_list:
                        if self._is_avialable_in_cache(self.dep_container,
                                                       dep_name):
                            self._update_from_cache(mc_main,
                                                    self.dep_container,
                                                    dep_name)

        return mc_metadata
