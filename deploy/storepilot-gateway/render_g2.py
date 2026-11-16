"""Add the shared authenticated rate check to the G1 Envoy configuration."""

import argparse
import json
from pathlib import Path

import yaml


def render(listener_port=8080, admin_port=9901):
    base = Path(__file__).with_name("envoy.yaml")
    config = yaml.safe_load(base.read_text(encoding="utf-8"))
    config["admin"]["address"]["socket_address"]["port_value"] = admin_port
    listener, = config["static_resources"]["listeners"]
    listener["address"]["socket_address"]["port_value"] = listener_port
    connection = listener["filter_chains"][0]["filters"][0]["typed_config"]
    connection["access_log"][0]["typed_config"]["log_format"]["json_format"]["gateway"] = str(listener_port)
    routes = connection["route_config"]["virtual_hosts"][0]["routes"]
    auth = "envoy.filters.http.ext_authz"
    disabled = {auth: {"@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthzPerRoute",
                       "disabled": True}}
    for route in routes:
        if route["name"] != "chat-stream":
            route["typed_per_filter_config"] = disabled
    routes.insert(0, {"name": "internal", "match": {"prefix": "/_gateway/"},
                      "direct_response": {"status": 404}, "typed_per_filter_config": disabled})
    connection["http_filters"].insert(-1, {"name": auth, "typed_config": {
        "@type": "type.googleapis.com/envoy.extensions.filters.http.ext_authz.v3.ExtAuthz",
        "status_on_error": {"code": "ServiceUnavailable"},
        "allowed_headers": {"patterns": [{"exact": "x-request-id"}]},
        "http_service": {
            "server_uri": {"uri": "http://app:5010", "cluster": "fastapi", "timeout": "2s"},
            "path_prefix": "/_gateway/rate",
            "authorization_response": {"allowed_client_headers": {"patterns": [
                {"exact": "content-type"}, {"exact": "retry-after"},
            ]}},
        },
    }})
    return json.dumps(config, indent=2)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    parser.add_argument("--listener-port", type=int, default=8080)
    parser.add_argument("--admin-port", type=int, default=9901)
    args = parser.parse_args()
    args.output.write_text(render(args.listener_port, args.admin_port) + "\n", encoding="utf-8")
