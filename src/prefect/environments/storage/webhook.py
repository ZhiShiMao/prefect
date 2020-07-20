# TODO
#
# * figure out how to work with systems where the ID isn't known
#   until after build() is done
# * test with Google Drive
# * test with something custom on API Gateway
# * test that creds work with Prefect Cloud secrets
# * test that creds work with environment variables
# * different secret_config for get_flow() vs. build()
# * add to all the relevant docs

import cloudpickle
import os
import warnings

from copy import deepcopy
from typing import TYPE_CHECKING, Any, Dict, List, Optional

from requests import Session
from requests.adapters import HTTPAdapter
from requests.models import Response
from requests.packages.urllib3.util.retry import Retry

from prefect.client import Secret
from prefect.environments.storage import Storage

if TYPE_CHECKING:
    from prefect.core.flow import Flow


class WebHook(Storage):
    """
    Args:
        - build_kwargs (dict): Dictionary of keyword arguments to the
            function from ``requests`` used to store the flow. Do not supply
            `"data"` to this argument, as it will be overwritten with the
            flow's content when `.build()` is run.
        - build_http_method (str): HTTP method identifying the type of request
            to execute when storing the flow. For example, ``"POST"`` for
            ``requests.post()``.
        - build_secret_config (dict): A dictionary describing how to set
            request headers from environment variables or Prefect Cloud
            secrets. See example for details on specifying this. This config
            applies to tthe request issued by `.build()`, and  wiill also be
            used for `.get_flow()` unless you explicitly set
            `get_flow_secret_config`.
        - get_flow_kwargs (dict): Dictionary of keyword arguments to the
            function from ``requests`` used to retrieve the flow.
        - get_flow_http_method (str): HTTP method identifying the type of
            request to execute when storing the flow. For example, ``"GET"``
            for ``requests.post()``.
        - get_flow_secret_config (dict): Similar to `build_secret_config`, but
            used for the request in `.get_flow()`. By default, the config
            passed to `build_secret_config` will be used for `.get_flow()`
            as well. Pass a value to this argument to override that behavior.

    Passing sensitive data in headers
    ---------------------------------

    For services which require authentication, use `secret_config` to pass
    sensitive data like API keys without storing their values in this Storage
    object.

    This should be a dictionary whose keys are headers, and whose
    values indicate whether to retrieve real values from environment
    variables (``"type": "environment"``) or
    Prefect secrets (``"type": "secret"``).

    So, for example, to get an API key from an environment variable you
    can do the following

    ```python
    storage = Webhoook(
        build_kwargs={
            "url": "some-random-service.place.thing",
            "headers" = {
                "Content-Type" = "application/octet-stream"
            }
        },
        build_http_method="POST",
        ...
        ...
        build_secret_config={
            "X-Api-Key": {
                "value": "MY_COOL_ENV_VARIABLE",
                "type": "environment"
            }
        }
    )
    ```
    """

    def __init__(
        self,
        build_kwargs: Dict[str, Any],
        build_http_method: str,
        get_flow_kwargs: Dict[str, Any],
        get_flow_http_method: str,
        build_secret_config: Optional[Dict[str, Any]] = None,
        get_flow_secret_config: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> None:
        self.flows = dict()  # type: Dict[str, str]
        self._flows = dict()  # type: Dict[str, "Flow"]

        # set up logic for authenticating with Saturn back-end service
        retry_logic = HTTPAdapter(max_retries=Retry(total=3))
        self._session = Session()
        self._session.mount("http://", retry_logic)
        self._session.mount("https://", retry_logic)

        self._method_to_function = {
            "GET": self._session.get,
            "PATCH": self._session.patch,
            "POST": self._session.post,
            "PUT": self._session.put,
        }

        if build_http_method not in self._method_to_function.keys():
            msg = "HTTP method '{}' not recognized".format(build_http_method)
            self.logger.fatal(msg)
            raise RuntimeError(msg)

        if get_flow_http_method not in self._method_to_function.keys():
            msg = "HTTP method '{}' not recognized".format(get_flow_http_method)
            self.logger.fatal(msg)
            raise RuntimeError(msg)

        self.build_kwargs = build_kwargs
        self.build_http_method = build_http_method
        self.build_secret_config = build_secret_config or {}

        self.get_flow_kwargs = get_flow_kwargs
        self.get_flow_http_method = get_flow_http_method
        self.get_flow_secret_config = get_flow_secret_config or self.build_secret_config

        self._build_responses: Optional[Dict[str, Response]] = None

        super().__init__(**kwargs)

    @property
    def default_labels(self) -> List[str]:
        return ["webhook-flow-storage"]

    def get_flow(self, flow_location: str = "placeholder") -> "Flow":
        """
        Get the flow from storage. This method will call
        `cloudpickle.loads()` on the binary content of the flow, so it
        shuould only be called in an environment with all of the flow's
        dependencies.

        Args:
            - flow_location (str): This argument is included to comply with the
                interface used by other storage objects, but it has no meaning
                for `WebHook` storage, since `WebHook` only corresponds to a
                single flow. Ignore it.

        Raises:
            - requests.exceptions.HTTPError if getting the flow fails
        """
        self.logger.info("Retrieving flow")
        req_function = self._method_to_function[self.get_flow_http_method]

        get_flow_kwargs = deepcopy(self.get_flow_kwargs)
        get_flow_kwargs["headers"] = self._render_headers(
            headers=get_flow_kwargs.get("headers", {}),
            secret_config=self.get_flow_secret_config,
        )

        response = req_function(**get_flow_kwargs)
        response.raise_for_status()

        return cloudpickle.loads(response.content)

    def add_flow(self, flow: "Flow") -> str:
        """
        Method for adding a flow to a `Storage` object's in-memory
        storage. `.build()` will look here for flows.

        `WebHook` storage only supports a single flow per storage
        object, so this method will overwrite any existing flows
        stored in an instance.

        Args:
            - flow (Flow): a Prefect Flow to add

        Returns:
            - str: the name of the flow
        """
        self.flows = {flow.name: flow.name}
        self._flows = {flow.name: flow}
        return flow.name

    def build(self) -> "WebHook":
        """
        Build the WebHook storage object by issuing an HTTP request
        to store the flow.

        The response from this request is stored in `._build_responses`,
        a dictionary keyed by flow name.

        Returns:
            - Storage: a WebHook storage object

        Raises:
            - requests.exceptions.HTTPError if pushing the flow fails
        """
        self.run_basic_healthchecks()
        self._build_responses = {}

        for flow_name, flow in self._flows.items():
            self.logger.info("Uploading flow '{}'".format(flow_name))

            data = cloudpickle.dumps(flow)

            req_function = self._method_to_function[self.build_http_method]

            build_kwargs = deepcopy(self.build_kwargs)
            build_kwargs["headers"] = self._render_headers(
                headers=build_kwargs.get("headers", {}),
                secret_config=self.build_secret_config,
            )

            if "data" in build_kwargs.keys():
                msg = (
                    "'data' found in build_kwargs. This value is overwritten with "
                    "the flow content and should not be set directly"
                )
                self.logger.warning(msg)
                warnings.warn(msg, RuntimeWarning)
            build_kwargs["data"] = data

            response = req_function(**build_kwargs)
            response.raise_for_status()

            self._build_responses[flow_name] = response
            self.logger.info("Successfully uploaded flow '{}'".format(flow_name))

        return self

    def __contains__(self, obj: Any) -> bool:
        """
        Method for determining whether an object is
        contained within this storage.
        """
        if not isinstance(obj, str):
            return False
        return obj in self.flows

    @staticmethod
    def _render_headers(
        headers: Dict[str, Any], secret_config: Dict[str, Any]
    ) -> Dict[str, Any]:
        """
        Given a dictionary of headers, add additional headers with values
        resolved froom environment variables or Prefect Cloud secrets.

        Args:
            - headers (dict): A dictionary of headers.
            - secret_config (dict): A secret config. See `help(WebHook)` for
                details on how this should be constructed.
        """
        out_headers = deepcopy(headers)
        for header, details in secret_config.items():
            name = details["value"]
            if details["type"] == "environment":
                out_headers[header] = os.environ[name]
            elif details["type"] == "secret":
                out_headers[header] = Secret(name).get()
        return out_headers
