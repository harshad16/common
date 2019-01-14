#!/usr/bin/env python3
# thoth-common
# Copyright(C) 2018 Fridolin Pokorny
#
# This program is free software: you can redistribute it and / or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE. See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program. If not, see <http://www.gnu.org/licenses/>.

"""Handling OpenShift and Kubernetes objects across project."""

import os
import logging
import requests
import typing
import json
import random

from .exceptions import NotFoundException
from .exceptions import ConfigurationError
from .helpers import (
    get_service_account_token,
    _get_incluster_token_file,
    _get_incluster_ca_file,
)

_LOGGER = logging.getLogger(__name__)


class OpenShift(object):
    """Interaction with OpenShift Master."""

    def __init__(
        self,
        *,
        frontend_namespace: str = None,
        middletier_namespace: str = None,
        backend_namespace: str = None,
        infra_namespace: str = None,
        kubernetes_api_url: str = None,
        kubernetes_verify_tls: bool = True,
        openshift_api_url: str = None,
        token: str = None,
        token_file: str = None,
        cert_file: str = None,
        environ=os.environ,
    ):
        """Initialize OpenShift class responsible for handling objects in deployment."""
        try:
            from kubernetes import client, config
            from openshift.dynamic import DynamicClient
            from kubernetes.config.incluster_config import InClusterConfigLoader
            from kubernetes.client.rest import RESTClientObject
        except ImportError as exc:
            raise ImportError(
                "Unable to import OpenShift and Kubernetes packages. Was thoth-common library "
                "installed with openshift extras?"
            ) from exc

        self.kubernetes_verify_tls = bool(
            int(os.getenv("KUBERNETES_VERIFY_TLS", 1)) and kubernetes_verify_tls
        )

        self.in_cluster = True
        # Try to load configuration as used in cluster. If not possible, try to load it from local configuration.
        try:
            # Load in-cluster configuration that is exposed by OpenShift/k8s configuration.
            InClusterConfigLoader(
                token_filename=_get_incluster_token_file(token_file),
                cert_filename=_get_incluster_ca_file(cert_file),
                environ=environ,
            ).load_and_set()

            # We need to explicitly set whether we want to verify SSL/TLS connection to the master.
            configuration = client.Configuration()
            configuration.verify_ssl = self.kubernetes_verify_tls
            self.ocp_client = DynamicClient(client.ApiClient(configuration=configuration))
        except Exception as exc:
            _LOGGER.warning("Failed to load in cluster configuration, fallback to a local development setup: %s", str(exc))
            k8s_client = config.new_client_from_config()
            k8s_client.configuration.verify_ssl = self.kubernetes_verify_tls
            k8s_client.rest_client = RESTClientObject(k8s_client.configuration)
            self.ocp_client = DynamicClient(k8s_client)
            self.in_cluster = False

        self.amun_inspection_namespace = frontend_namespace or os.getenv(
            "THOTH_AMUN_INSPECTION_NAMESAPCE"
        )
        self.frontend_namespace = frontend_namespace or os.getenv(
            "THOTH_FRONTEND_NAMESPACE"
        )
        self.middletier_namespace = middletier_namespace or os.getenv(
            "THOTH_MIDDLETIER_NAMESPACE"
        )
        self.backend_namespace = backend_namespace or os.getenv(
            "THOTH_BACKEND_NAMESPACE"
        )
        self.infra_namespace = infra_namespace or os.getenv("THOTH_INFRA_NAMESPACE")
        self.kubernetes_api_url = kubernetes_api_url or os.getenv(
            "KUBERNETES_API_URL", "https://kubernetes.default.svc.cluster.local"
        )
        self.openshift_api_url = openshift_api_url or os.getenv(
            "OPENSHIFT_API_URL", self.ocp_client.configuration.host
        )
        self._token = token

    @property
    def token(self):
        """Access service account token mounted to the pod."""
        if self._token is None:
            if self.in_cluster:
                self._token = get_service_account_token()
            else:
                # Ugly, but k8s client does not expose a nice API for this.
                self._token = self.ocp_client.configuration.auth_settings()['BearerToken']['value'].split(' ')[1]

        return self._token

    @staticmethod
    def _set_env_var(template: dict, **env_var):
        """Set environment in the given template."""
        for env_var_name, env_var_value in env_var.items():
            for entry in template["spec"]["containers"][0]["env"]:
                if entry["name"] == env_var_name:
                    entry["value"] = env_var_value
                    break
            else:
                template["spec"]["containers"][0]["env"].append(
                    {"name": env_var_name, "value": str(env_var_value)}
                )

    @staticmethod
    def set_template_parameters(template: dict, **parameters: object) -> None:
        """Set parameters in the template - replace existing ones or append to parameter list if not exist.

        >>> set_template_parameters(template, THOTH_LOG_ADVISER='DEBUG')
        """
        _LOGGER.debug(
            "Setting parameters for template %r: %s",
            template["metadata"]["name"],
            parameters,
        )

        if "parameters" not in template:
            template["parameters"] = []

        for parameter_name, parameter_value in parameters.items():
            for entry in template["parameters"]:
                if entry["name"] == parameter_name:
                    entry["value"] = (
                        str(parameter_value) if parameter_value is not None else ""
                    )
                    break
            else:
                _LOGGER.warning(
                    "Requested to assign parameter %r (value %r) to template but template "
                    "does not provide the given parameter, forcing...",
                    parameter_name,
                    parameter_value,
                )
                template["parameters"].append(
                    {
                        "name": parameter_name,
                        "value": str(parameter_value)
                        if parameter_value is not None
                        else "",
                    }
                )

    def run_sync(self, force_sync: bool = False) -> str:
        """Run graph sync, base pod definition based on job definition."""
        # Let's reuse pod definition from the cronjob definition so any changes in
        # deployed application work out of the box.
        if not self.frontend_namespace:
            raise ConfigurationError(
                "Graph sync requires frontend namespace configuration"
            )

        _LOGGER.debug("Retrieving graph-sync CronJob definition")
        response = self.ocp_client.resources.get(
            api_version="v2alpha1", kind="CronJob"
        ).get(namespace=self.frontend_namespace, name="graph-sync")
        template = response.to_dict()
        labels = template["metadata"]["labels"]
        labels.pop("template", None)  # remove template label
        job_template = template["spec"]["jobTemplate"]["spec"]["template"]
        self._set_env_var(job_template, THOTH_FORCE_SYNC=int(force_sync))

        # Construct a Pod spec.
        pod_template = {
            "apiVersion": "v1",
            "kind": "Pod",
            "metadata": {"generateName": "graph-sync-", "labels": labels},
            "spec": job_template["spec"],
        }

        response = self.ocp_client.resources.get(api_version="v1", kind="Pod").create(
            body=pod_template, namespace=self.frontend_namespace
        )

        _LOGGER.debug(f"Started graph-sync pod with name {response.metadata.name}")
        return response.metadata.name

    def get_pod_log(self, pod_id: str, namespace: str = None) -> typing.Optional[str]:
        """Get log of a pod based on assigned pod ID."""
        if not namespace:
            if not self.middletier_namespace:
                raise ConfigurationError(
                    "Middletier namespace is required to check log of pods run in this namespace"
                )
            namespace = self.middletier_namespace

        # TODO: rewrite to OpenShift rest client once it will support it.
        endpoint = "{}/api/v1/namespaces/{}/pods/{}/log".format(
            self.kubernetes_api_url, namespace, pod_id
        )

        response = requests.get(
            endpoint,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Content-Type": "application/json",
            },
            verify=self.kubernetes_verify_tls,
        )
        _LOGGER.debug(
            "Kubernetes master response for pod log (%d): %r",
            response.status_code,
            response.text,
        )

        if response.status_code == 404:
            raise NotFoundException(
                f"Pod with id {pod_id} was not found in namespace {namespace}"
            )

        if response.status_code == 400:
            # If Pod has not been initialized yet, there is returned 400 status code. Return None in this case.
            return None

        response.raise_for_status()
        return response.text

    def get_build(self, build_id: str, namespace: str) -> dict:
        """Get a build in the given namespace."""
        # TODO: rewrite to OpenShift rest client once it will support it.
        endpoint = "{}/apis/build.openshift.io/v1/namespaces/{}/builds/{}".format(
            self.openshift_api_url, namespace, build_id
        )

        response = requests.get(
            endpoint,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Content-Type": "application/json",
            },
            verify=self.kubernetes_verify_tls,
        )

        if response.status_code == 404:
            raise NotFoundException(
                f"Build with id {build_id} was not found in namespace {namespace}"
            )

        _LOGGER.debug(
            "OpenShift master response for build (%d): %r",
            response.status_code,
            response.text,
        )
        response.raise_for_status()

        return response.json()

    def get_buildconfig(self, buildconfig_id: str, namespace: str) -> dict:
        """Get a buildconfig in the given namespace."""
        # TODO: rewrite to OpenShift rest client once it will support it.
        endpoint = "{}/apis/build.openshift.io/v1/namespaces/{}/buildconfigs/{}".format(
            self.openshift_api_url, namespace, buildconfig_id
        )

        response = requests.get(
            endpoint,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Content-Type": "application/json",
            },
            verify=self.kubernetes_verify_tls,
        )

        if response.status_code == 404:
            raise NotFoundException(
                f"BuildConfig with id {buildconfig_id} was not found in namespace {namespace}"
            )

        _LOGGER.debug(
            "OpenShift master response for build (%d): %r",
            response.status_code,
            response.text,
        )
        response.raise_for_status()

        return response.json()

    def get_build_log(self, build_id: str, namespace: str) -> str:
        """Get log of a build in the given namespace."""
        # TODO: rewrite to OpenShift rest client once it will support it.
        endpoint = "{}/apis/build.openshift.io/v1/namespaces/{}/builds/{}/log".format(
            self.openshift_api_url, namespace, build_id
        )

        response = requests.get(
            endpoint,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Content-Type": "application/json",
            },
            verify=self.kubernetes_verify_tls,
        )

        if response.status_code == 404:
            raise NotFoundException(
                f"Build with id {build_id} was not found in namespace {namespace}"
            )

        _LOGGER.debug(
            "OpenShift master response for build log (%d): %r",
            response.status_code,
            response.text,
        )
        response.raise_for_status()

        return response.text

    def get_pod_status(self, pod_id: str, namespace: str) -> dict:
        """Get status entry for a pod - low level routine."""
        import openshift

        try:
            response = self.ocp_client.resources.get(api_version="v1", kind="Pod").get(
                namespace=namespace, name=pod_id
            )
        except openshift.dynamic.exceptions.NotFoundError as exc:
            raise NotFoundException(
                f"The given pod with id {pod_id} could not be found"
            ) from exc

        response = response.to_dict()
        _LOGGER.debug("OpenShift master response for pod status: %r", response)

        if "containerStatuses" not in response["status"]:
            # No status - pod is being scheduled.
            return {}

        state = response["status"]["containerStatuses"][0]["state"]
        # Translate kills of liveness probes to our messages reported to user.
        if (
            state.get("terminated", {}).get("exitCode") == 137
            and state["terminated"]["reason"] == "Error"
        ):
            # Reason can be set by OpenShift to be OOMKilled for example - we expect only "Error" to be set to
            # treat this as timeout.
            state["terminated"]["reason"] = "TimeoutKilled"

        return state

    @staticmethod
    def _status_report(status):
        """Construct status response for API response from master API response."""
        _TRANSLATION_TABLE = {
            "exitCode": "exit_code",
            "finishedAt": "finished_at",
            "reason": "reason",
            "startedAt": "started_at",
            "containerID": "container",
            "message": "reason",
        }

        if len(status.keys()) != 1:
            # If pod was not initialized yet and user asks for status, return default values with state scheduling.
            reported_status = dict.fromkeys(tuple(_TRANSLATION_TABLE.values()))
            reported_status["state"] = "scheduling"
            return reported_status

        state = list(status.keys())[0]

        reported_status = dict.fromkeys(tuple(_TRANSLATION_TABLE.values()))
        reported_status["state"] = state
        for key, value in status[state].items():
            if key == "containerID":
                value = (
                    value[len("docker://") :]
                    if value.startswith("docker://")
                    else value
                )
                reported_status["container"] = value
            else:
                reported_status[_TRANSLATION_TABLE[key]] = value

        return reported_status

    def get_pod_status_report(self, pod_id: str, namespace: str) -> dict:
        """Get pod state and convert it to a user-friendly response."""
        state = self.get_pod_status(pod_id, namespace)
        return self._status_report(state)

    def _get_pod_id_from_job(self, job_id: str, namespace: str) -> str:
        """Get pod name from a job."""
        # Kubernetes automatically adds 'job-name' label -> reuse it.
        response = self.ocp_client.resources.get(api_version="v1", kind="Pod").get(
            namespace=namespace or self.infra_namespace,
            label_selector=f"job-name={job_id}",
        )
        response = response.to_dict()
        _LOGGER.debug("OpenShift response for pod id from job: %r", response)

        if len(response["items"]) != 1:
            if len(response["items"]) > 1:
                # Log this error and report back to user not found.
                _LOGGER.error(
                    f"Multiple pods for the same job name selector {job_id} found"
                )

            raise NotFoundException(f"Job with the given id {job_id} was not found")

        return response["items"][0]["metadata"]["name"]

    def get_job_status_report(self, job_id: str, namespace: str) -> dict:
        """Get status of a pod running inside a job."""
        pod_id = self._get_pod_id_from_job(job_id, namespace)
        return self.get_pod_status_report(pod_id, namespace)

    def get_job_log(self, job_id: str, namespace: str = None) -> str:
        """Get log of a pod running inside a job."""
        pod_id = self._get_pod_id_from_job(job_id, namespace)
        return self.get_pod_log(pod_id, namespace)

    def get_jobs(self, label_selector: str, namespace: str = None) -> dict:
        """Get all Jobs, select them by the provided label."""
        import openshift

        response = None
        try:
            response = self.ocp_client.resources.get(
                api_version="batch/v1", kind="JobList"
            ).get(namespace=namespace, label_selector=label_selector)
        except openshift.dynamic.exceptions.NotFoundError as exc:
            raise NotFoundException(
                f"No Jobs with label {label_selector} could be found"
            ) from exc

        _LOGGER.debug("OpenShift response: %r", response)

        return response

    def create_inspection_imagestream(self, inspection_id: str) -> str:
        """Create imagestream for Amun."""
        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required in order to create inspect imagestreams"
            )

        if not self.amun_inspection_namespace:
            raise ConfigurationError(
                "Unable to create inspection image stream without Amun inspection namespace being set"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace,
            label_selector="template=amun-inspect-imagestream",
        )

        self._raise_on_invalid_response_size(response)

        response = response.to_dict()
        _LOGGER.debug(
            "OpenShift response for getting Amun inspect ImageStream template: %r",
            response,
        )
        template = response["items"][0]

        self.set_template_parameters(template, AMUN_INSPECTION_ID=inspection_id)
        template = self.oc_process(self.infra_namespace, template)
        imagestream = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=imagestream["apiVersion"], kind=imagestream["kind"]
        ).create(body=imagestream, namespace=self.amun_inspection_namespace)

        response = response.to_dict()
        _LOGGER.debug("OpenShift response for creating Amun ImageStream: %r", response)

        return response["metadata"]["name"]

    def create_inspection_buildconfig(
        self, parameters: dict, use_hw_template: bool
    ) -> None:
        """Create a build config for Amun."""
        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required in order to create inspect imagestreams"
            )

        if not self.amun_inspection_namespace:
            raise ConfigurationError(
                "Unable to create inspection buildconfig without Amun inspection namespace being set"
            )

        if use_hw_template:
            label_selector = "template=amun-inspect-buildconfig-with-cpu"
        else:
            label_selector = "template=amun-inspect-buildconfig"

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector=label_selector
        )

        self._raise_on_invalid_response_size(response)
        response = response.to_dict()
        _LOGGER.debug(
            "OpenShift response for getting Amun inspect BuildConfig template: %r",
            response,
        )

        template = response["items"][0]

        self.set_template_parameters(template, **parameters)

        template = self.oc_process(self.amun_inspection_namespace, template)
        buildconfig = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=buildconfig["apiVersion"], kind=buildconfig["kind"]
        ).create(body=buildconfig, namespace=self.amun_inspection_namespace)

        _LOGGER.debug(
            "OpenShift response for creating Amun BuildConfig: %r", response.to_dict()
        )

    def schedule_inspection_job(
        self, inspection_id, parameters: dict, use_hw_template: bool
    ) -> str:
        """Schedule the given job run, the scheduled job is handled by workload operator based resources available."""
        if not self.amun_inspection_namespace:
            raise ConfigurationError(
                "Unable to schedule inspection job without Amun inspection namespace being set"
            )

        parameters = locals()
        parameters.pop("self", None)
        parameters.pop("inspection_id", None)
        return self._schedule_job(
            self.run_inspection_job.__name__,
            parameters,
            inspection_id,
            self.amun_inspection_namespace,
        )

    def run_inspection_job(self, parameters: dict, use_hw_template: bool) -> None:
        """Create the actual inspect job."""
        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required in order to create inspect imagestreams"
            )

        if not self.amun_inspection_namespace:
            raise ConfigurationError(
                "Unable to create inspection job without Amun inspection namespace being set"
            )

        if use_hw_template:
            label_selector = "template=amun-inspect-job-with-cpu"
        else:
            label_selector = "template=amun-inspect-job"

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector=label_selector
        )

        self._raise_on_invalid_response_size(response)
        response = response.to_dict()
        _LOGGER.debug(
            "OpenShift response for getting Amun inspect Job template: %r", response
        )

        template = response["items"][0]
        self.set_template_parameters(template, **parameters)

        template = self.oc_process(self.amun_inspection_namespace, template)
        job = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=job["apiVersion"], kind=job["kind"]
        ).create(body=job, namespace=self.amun_inspection_namespace)

        _LOGGER.debug(
            "OpenShift response for creating Amun Job: %r", response.to_dict()
        )

    def get_solver_names(self) -> list:
        """Retrieve name of solvers available in installation."""
        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required in order to list solvers"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector="template=solver"
        )
        _LOGGER.debug(
            "OpenShift response for getting solver template: %r", response.to_dict()
        )
        self._raise_on_invalid_response_size(response)
        return [
            obj["metadata"]["labels"]["component"]
            for obj in response.to_dict()["items"][0]["objects"]
        ]

    def run_solver(
        self,
        packages: str,
        output: str,
        indexes: list = None,
        debug: bool = False,
        subgraph_check_api: str = None,
        transitive: bool = True,
        solver: str = None,
    ) -> dict:
        """Run solver or all solver to solve the given requirements."""
        if not self.middletier_namespace:
            ConfigurationError("Solver requires middletier namespace to be specified")

        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required to gather solver template when running solver"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector="template=solver"
        )
        _LOGGER.debug(
            "OpenShift response for getting solver template: %r", response.to_dict()
        )

        self._raise_on_invalid_response_size(response)
        template = response.to_dict()["items"][0]

        self.set_template_parameters(
            template,
            THOTH_SOLVER_NO_TRANSITIVE=int(not transitive),
            THOTH_SOLVER_PACKAGES=packages.replace("\n", "\\n"),
            THOTH_SOLVER_INDEXES=",".join(indexes) if indexes else "",
            THOTH_LOG_SOLVER="DEBUG" if debug else "INFO",
            THOTH_SOLVER_OUTPUT=output,
            THOTH_SOLVER_SUBGRAPH_CHECK_API=subgraph_check_api,
        )

        template = self.oc_process(self.middletier_namespace, template)

        solvers = {}
        for obj in template["objects"]:
            solver_name = obj["metadata"]["labels"]["component"]
            if solver and solver != solver_name:
                _LOGGER.debug(
                    f"Skipping solver %r as the requested solver is %r",
                    solver_name,
                    solver,
                )
                continue

            response = self.ocp_client.resources.get(
                api_version=obj["apiVersion"], kind=obj["kind"]
            ).create(body=obj, namespace=self.middletier_namespace)

            _LOGGER.debug("Starting solver %r", solver_name)
            _LOGGER.debug(
                "OpenShift response for creating a pod: %r", response.to_dict()
            )
            solvers[solver_name] = response.metadata.name

        return solvers

    def run_package_extract(
        self,
        image: str,
        output: str,
        registry_user: str = None,
        registry_password: str = None,
        verify_tls: bool = True,
        debug: bool = False,
    ) -> str:
        """Run package-extract analyzer to extract information from the provided image."""
        if not self.middletier_namespace:
            raise ConfigurationError(
                "Running package-extract requires middletier namespace to be specified"
            )

        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required to gather package-extract template when running it"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector="template=package-extract"
        )
        _LOGGER.debug(
            "OpenShift response for getting package-extract template: %r",
            response.to_dict(),
        )
        self._raise_on_invalid_response_size(response)
        template = response.to_dict()["items"][0]

        self.set_template_parameters(
            template,
            THOTH_LOG_PACKAGE_EXTRACT="DEBUG" if debug else "INFO",
            THOTH_ANALYZED_IMAGE=image,
            THOTH_ANALYZER_NO_TLS_VERIFY=int(not verify_tls),
            THOTH_ANALYZER_OUTPUT=output,
        )

        if registry_user and registry_password:
            self.set_template_parameters(
                template,
                THOTH_REGISTRY_CREDENTIALS=f"{registry_user}:{registry_password}",
            )

        template = self.oc_process(self.middletier_namespace, template)
        analyzer = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=analyzer["apiVersion"], kind=analyzer["kind"]
        ).create(body=analyzer, namespace=self.middletier_namespace)

        _LOGGER.debug("OpenShift response for creating a pod: %r", response.to_dict())
        return response.metadata.name

    def create_config_map(
        self, configmap_name: str, namespace: str, labels: dict, data: dict
    ) -> str:
        """Create a ConfigMap in the given namespace."""
        v1_configmaps = self.ocp_client.resources.get(
            api_version="v1", kind="ConfigMap"
        )
        v1_configmaps.create(
            body={
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "data": data,
                "metadata": {
                    "labels": labels,
                    "name": configmap_name,
                    "namespace": namespace,
                },
            }
        )
        return configmap_name

    def _schedule_job(
        self, method_name: str, parameters: dict, job_id: str, namespace: str
    ) -> str:
        """Schedule the given job run, the scheduled job is handled by workload operator based resources available."""
        self.create_config_map(
            job_id,
            namespace,
            labels={"app": "thoth", "operator": "workload"},
            data={"method": method_name, "parameters": json.dumps(parameters)},
        )
        return job_id

    @staticmethod
    def _generate_id(prefix: str):
        """Generate an identifier"""
        return prefix + "-%016x" % random.getrandbits(64)

    def schedule_dependency_monkey(
        self,
        requirements: typing.Union[str, dict],
        context: dict,
        *,
        stack_output: str = None,
        report_output: str = None,
        seed: int = None,
        dry_run: bool = False,
        decision: str = None,
        count: int = None,
        debug: bool = False,
        job_id: str = None,
    ) -> str:
        """Schedule a dependency monkey run."""
        if not self.middletier_namespace:
            raise ConfigurationError(
                "Unable to schedule dependency monkey without middletier namespace being set"
            )

        job_id = job_id or self._generate_id("dependency-monkey")
        parameters = locals()
        parameters.pop("self", None)
        return self._schedule_job(
            self.run_dependency_monkey.__name__,
            parameters,
            job_id,
            self.middletier_namespace,
        )

    def run_dependency_monkey(
        self,
        requirements: typing.Union[str, dict],
        context: dict,
        *,
        stack_output: str = None,
        report_output: str = None,
        seed: int = None,
        dry_run: bool = False,
        decision: str = None,
        count: int = None,
        debug: bool = False,
        job_id: str = None,
    ) -> str:
        """Run Dependency Monkey on the provided user input."""
        if not self.middletier_namespace:
            raise ConfigurationError(
                "Running Dependency Monkey requires middletier namespace configuration"
            )

        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required to gather Dependency Monkey template when running it"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector="template=dependency-monkey"
        )
        _LOGGER.debug(
            "OpenShift response for getting dependency-monkey template: %r",
            response.to_dict(),
        )
        self._raise_on_invalid_response_size(response)

        if isinstance(requirements, dict):
            # There was passed a serialized Pipfile into dict, serialize it into JSON.
            requirements = json.dumps(requirements)

        template = response.to_dict()["items"][0]
        parameters = {
            "THOTH_ADVISER_REQUIREMENTS": requirements.replace("\n", "\\n"),
            "THOTH_AMUN_CONTEXT": json.dumps(context).replace("\n", "\\n"),
            "THOTH_DEPENDENCY_MONKEY_STACK_OUTPUT": stack_output or "-",
            "THOTH_DEPENDENCY_MONKEY_REPORT_OUTPUT": report_output or "-",
            "THOTH_DEPENDENCY_MONKEY_DRY_RUN": int(bool(dry_run)),
            "THOTH_LOG_ADVISER": "DEBUG" if debug else "INFO",
            "THOTH_DEPENDENCY_MONKEY_JOB_ID": job_id
            or self._generate_id("dependency-monkey"),
        }

        if decision is not None:
            parameters["THOTH_DEPENDENCY_MONKEY_DECISION"] = decision

        if seed is not None:
            parameters["THOTH_DEPENCENCY_MONKEY_SEED"] = seed

        if count is not None:
            parameters["THOTH_DEPENDENCY_MONKEY_COUNT"] = count

        self.set_template_parameters(template, **parameters)

        template = self.oc_process(self.middletier_namespace, template)
        dependency_monkey = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=dependency_monkey["apiVersion"], kind=dependency_monkey["kind"]
        ).create(body=dependency_monkey, namespace=self.middletier_namespace)

        _LOGGER.debug("OpenShift response for creating a pod: %r", response.to_dict())
        return response.metadata.name

    def schedule_adviser(
        self,
        application_stack: dict,
        output: str,
        recommendation_type: str,
        *,
        count: int = None,
        limit: int = None,
        runtime_environment: dict = None,
        debug: bool = False,
        job_id: str = None,
    ) -> str:
        """Schedule an adviser run."""
        if not self.backend_namespace:
            raise ConfigurationError(
                "Unable to schedule adviser without backend namespace being set"
            )

        job_id = job_id or self._generate_id("adviser")
        parameters = locals()
        parameters.pop("self", None)
        return self._schedule_job(
            self.run_adviser.__name__, parameters, job_id, self.backend_namespace
        )

    def run_adviser(
        self,
        application_stack: dict,
        output: str,
        recommendation_type: str,
        count: int = None,
        limit: int = None,
        runtime_environment: dict = None,
        debug: bool = False,
        job_id: str = None,
    ) -> str:
        """Run adviser on the provided user input."""
        if not self.backend_namespace:
            raise ConfigurationError(
                "Running adviser requires backend namespace configuration"
            )

        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required to gather adviser template when running it"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector="template=adviser"
        )
        _LOGGER.debug(
            "OpenShift response for getting adviser template: %r", response.to_dict()
        )
        self._raise_on_invalid_response_size(response)

        if runtime_environment:
            runtime_environment = json.dumps(runtime_environment)

        parameters = {
            "THOTH_ADVISER_REQUIREMENTS": application_stack.pop("requirements").replace(
                "\n", "\\n"
            ),
            "THOTH_ADVISER_REQUIREMENTS_LOCKED": application_stack.get(
                "requirements_lock", ""
            ).replace("\n", "\\n"),
            "THOTH_ADVISER_REQUIREMENTS_FORMAT": application_stack.get(
                "requirements_formant", "pipenv"
            ),
            "THOTH_ADVISER_RECOMMENDATION_TYPE": recommendation_type,
            "THOTH_ADVISER_RUNTIME_ENVIRONMENT": runtime_environment,
            "THOTH_ADVISER_OUTPUT": output,
            "THOTH_LOG_ADVISER": "DEBUG" if debug else "INFO",
            "THOTH_ADVISER_JOB_ID": job_id or self._generate_id("adviser"),
        }

        if count:
            parameters["THOTH_ADVISER_COUNT"] = count

        if limit:
            parameters["THOTH_ADVISER_LIMIT"] = limit

        template = response.to_dict()["items"][0]
        self.set_template_parameters(template, **parameters)

        template = self.oc_process(self.backend_namespace, template)
        adviser = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=adviser["apiVersion"], kind=adviser["kind"]
        ).create(body=adviser, namespace=self.backend_namespace)

        _LOGGER.debug("OpenShift response for creating a pod: %r", response.to_dict())
        return response.metadata.name

    def schedule_provenance_checker(
        self,
        application_stack: dict,
        output: str,
        *,
        whitelisted_sources: list = None,
        debug: bool = False,
        job_id: str = None,
    ) -> str:
        """Schedule a provenance checker run."""
        if not self.backend_namespace:
            raise ConfigurationError(
                "Unable to schedule provenance checker without backend namespace being set"
            )

        job_id = job_id or self._generate_id("provenance-checker")
        parameters = locals()
        parameters.pop("self", None)
        return self._schedule_job(
            self.run_provenance_checker.__name__,
            parameters,
            job_id,
            self.backend_namespace,
        )

    def run_provenance_checker(
        self,
        application_stack: dict,
        output: str,
        *,
        whitelisted_sources: list = None,
        debug: bool = False,
        job_id: str = None,
    ) -> str:
        """Run provenance checks on the provided user input."""
        if not self.backend_namespace:
            raise ConfigurationError(
                "Running provenance checks requires backend namespace configuration"
            )

        if not self.infra_namespace:
            raise ConfigurationError(
                "Infra namespace is required to gather provenance template when running it"
            )

        response = self.ocp_client.resources.get(api_version="v1", kind="Template").get(
            namespace=self.infra_namespace, label_selector="template=provenance-checker"
        )
        _LOGGER.debug(
            "OpenShift response for getting provenance-checker template: %r",
            response.to_dict(),
        )
        self._raise_on_invalid_response_size(response)

        requirements = application_stack.pop("requirements").replace("\n", "\\n")
        requirements_locked = application_stack.pop("requirements_lock").replace(
            "\n", "\\n"
        )
        whitelisted_sources = ",".join(whitelisted_sources or [])
        template = response.to_dict()["items"][0]
        self.set_template_parameters(
            template,
            THOTH_ADVISER_REQUIREMENTS=requirements,
            THOTH_ADVISER_REQUIREMENTS_LOCKED=requirements_locked,
            THOTH_ADVISER_OUTPUT=output,
            THOTH_WHITELISTED_SOURCES=whitelisted_sources,
            THOTH_LOG_ADVISER="DEBUG" if debug else "INFO",
            THOTH_PROVENANCE_CHECKER_JOB_ID=job_id
            or self._generate_id("provenance-checker"),
        )

        template = self.oc_process(self.backend_namespace, template)
        provenance_checker = template["objects"][0]

        response = self.ocp_client.resources.get(
            api_version=provenance_checker["apiVersion"],
            kind=provenance_checker["kind"],
        ).create(body=provenance_checker, namespace=self.backend_namespace)

        _LOGGER.debug("OpenShift response for creating a pod: %r", response.to_dict())
        return response.metadata.name

    def _raise_on_invalid_response_size(self, response):
        """It is expected that there is only one object type for the given item."""
        if len(response.items) != 1:
            raise RuntimeError(
                f"Application misconfiguration - number of templates available in the infra namespace "
                f"{self.infra_namespace!r} is {len(response.items)}, should be 1."
            )

    def oc_process(self, namespace: str, template: dict) -> dict:
        """Process the given template in OpenShift."""
        # TODO: This does not work - see issue reported upstream:
        #   https://github.com/openshift/openshift-restclient-python/issues/190
        # return TemplateOpenshiftIoApi().create_namespaced_processed_template_v1(namespace, template)
        endpoint = "{}/apis/template.openshift.io/v1/namespaces/{}/processedtemplates".format(
            self.openshift_api_url, namespace
        )
        response = requests.post(
            endpoint,
            json=template,
            headers={
                "Authorization": "Bearer {}".format(self.token),
                "Content-Type": "application/json",
            },
            verify=self.kubernetes_verify_tls,
        )
        _LOGGER.debug(
            "OpenShift master response template (%d): %r",
            response.status_code,
            response.text,
        )

        try:
            response.raise_for_status()
        except Exception:
            _LOGGER.error("Failed to process template: %s", response.text)
            raise

        return response.json()
