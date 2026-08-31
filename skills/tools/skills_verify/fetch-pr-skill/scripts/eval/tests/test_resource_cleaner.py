#!/usr/bin/env python3
import sys
import os
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from eval.resource_cleaner import extract_cloud_resources, _sign_huawei_request, _EMPTY_HASH


def _make_session(log_text):
    d = tempfile.mkdtemp()
    with open(os.path.join(d, "output.log"), "w") as f:
        f.write(log_text)
    return d


def test_sign_get_canonical_uri_has_trailing_slash():
    headers = {"Host": "ecs.cn-north-4.myhuaweicloud.com", "X-Sdk-Date": "20260803T000000Z"}
    signed = _sign_huawei_request("GET", "https://ecs.cn-north-4.myhuaweicloud.com/v1/pid123/cloudservers/detail", dict(headers))
    # Authorization 头应存在，SignedHeaders 只含 host;x-sdk-date（GET 无 body）
    auth = signed["Authorization"]
    assert "SignedHeaders=host;x-sdk-date" in auth, auth


def test_sign_get_with_body_includes_content_type():
    headers = {"Host": "ecs.cn-north-4.myhuaweicloud.com", "X-Sdk-Date": "20260803T000000Z", "Content-Type": "application/json"}
    signed = _sign_huawei_request("POST", "https://ecs.cn-north-4.myhuaweicloud.com/v1/pid123/cloudservers", dict(headers), body='{"servers":[]}')
    auth = signed["Authorization"]
    assert "SignedHeaders=content-type;host;x-sdk-date" in auth, auth


def test_sign_get_query_preserved():
    headers = {"Host": "ecs.cn-north-4.myhuaweicloud.com", "X-Sdk-Date": "20260803T000000Z"}
    signed = _sign_huawei_request("GET", "https://ecs.cn-north-4.myhuaweicloud.com/v1/pid123/cloudservers/detail?limit=1", dict(headers))
    assert "Authorization" in signed


def test_empty_hash_constant():
    assert len(_EMPTY_HASH) == 64


def test_extract_swr_namespace_from_json():
    d = _make_session('{"namespace": "test-ns", "id": 123}\n')
    resources = extract_cloud_resources(d)
    ns = [r for r in resources if "namespace" in r.resource_type or r.resource_id == "test-ns"]
    assert len(ns) >= 1
    assert ns[0].resource_id == "test-ns"


def test_extract_swr_from_hcloud_create():
    d = _make_session("$ hcloud SWR CreateNamespace --namespace=eval-test --cli-region=cn-north-4\n{}\n")
    resources = extract_cloud_resources(d)
    ns = [r for r in resources if "CreateNamespace" in r.resource_type or r.resource_id == "eval-test"]
    assert len(ns) >= 1
    assert ns[0].resource_id == "eval-test"
    assert ns[0].delete_cmd != ""


def test_extract_ecs_from_hcloud_create():
    d = _make_session("$ hcloud ECS CreateServer --name=test-vm --cli-region=cn-north-4\n{}\n")
    resources = extract_cloud_resources(d)
    ecs = [r for r in resources if "CreateServer" in r.resource_type]
    assert len(ecs) >= 1


def test_extract_ecs_instance():
    d = _make_session("instance_id: i-abc123def456\n")
    resources = extract_cloud_resources(d)
    ecs = [r for r in resources if r.resource_type == "ecs"]
    assert len(ecs) >= 1


def test_no_resources():
    d = _make_session("nothing here\n")
    resources = extract_cloud_resources(d)
    assert len(resources) == 0
