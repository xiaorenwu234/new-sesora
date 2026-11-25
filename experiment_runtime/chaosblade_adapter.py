"""ChaosBlade Operator 适配器：通过 CRD 注入/回收故障（主故障执行器）。

只使用预定义动作类型到 ChaosBlade 场景的固定映射，
不接受 Agent 传入的任意命令（方案 §4.5 权限边界）。
"""
from __future__ import annotations

import time
import uuid

from kubernetes import client

from common.config import Config
from collectors.deployment.k8s_collector import load_apis

GROUP, VERSION, PLURAL = "chaosblade.io", "v1alpha1", "chaosblades"


class ChaosBladeAdapter:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        _, _, _, self.custom = load_apis(cfg)

    def _body(self, name: str, experiments: list[dict]) -> dict:
        return {"apiVersion": f"{GROUP}/{VERSION}", "kind": "ChaosBlade",
                "metadata": {"name": name},
                "spec": {"experiments": experiments}}

    def inject(self, action_type: str, namespace: str, labels: dict,
               params: dict, service_port: int | None = None) -> str:
        name = f"cnaa-{action_type.lower().replace('_', '-')}-{uuid.uuid4().hex[:8]}"
        label_strs = [f"{k}={v}" for k, v in labels.items()]
        base = [{"name": "namespace", "value": [namespace]},
                {"name": "labels", "value": label_strs}]
        if action_type == "NETWORK_DELAY":
            m = base + [{"name": "interface", "value": [params.get("interface", "eth0")]},
                        {"name": "time", "value": [str(params.get("time_ms", 3000))]}]
            if service_port:
                m.append({"name": "local-port", "value": [str(service_port)]})
            exp = {"scope": "pod", "target": "network", "action": "delay",
                   "desc": "cnaa controlled network delay", "matchers": m}
        elif action_type in ("NETWORK_LOSS", "DEPENDENCY_UNAVAILABLE"):
            m = base + [{"name": "interface", "value": [params.get("interface", "eth0")]},
                        {"name": "percent", "value": [str(params.get("percent", 60))]}]
            if service_port:
                m.append({"name": "local-port", "value": [str(service_port)]})
            exp = {"scope": "pod", "target": "network", "action": "loss",
                   "desc": "cnaa controlled network loss", "matchers": m}
        elif action_type == "POD_TERMINATION":
            m = base + [{"name": "evict-count",
                         "value": [str(params.get("evict_count", 1))]}]
            exp = {"scope": "pod", "target": "pod", "action": "fail",
                   "desc": "cnaa controlled pod failure", "matchers": m}
        elif action_type == "CPU_PRESSURE":
            m = base + [{"name": "cpu-percent",
                         "value": [str(params.get("cpu_percent", 80))]}]
            exp = {"scope": "pod", "target": "cpu", "action": "fullload",
                   "desc": "cnaa controlled cpu load", "matchers": m}
        elif action_type == "MEMORY_PRESSURE":
            m = base + [{"name": "mem-percent",
                         "value": [str(params.get("mem_percent", 70))]}]
            exp = {"scope": "pod", "target": "mem", "action": "load",
                   "desc": "cnaa controlled memory load", "matchers": m}
        elif action_type in ("JVM_DELAY", "JVM_EXCEPTION"):
            from experiment_runtime.jvm_injection_adapter import JvmInjectionAdapter
            return JvmInjectionAdapter(self.cfg).inject(
                action_type, namespace, labels, params, service_port)
        else:
            raise ValueError(f"unsupported fault action: {action_type}")

        self.custom.create_cluster_custom_object(
            GROUP, VERSION, PLURAL, self._body(name, [exp]))
        return name

    def status(self, name: str) -> dict:
        try:
            return self.custom.get_cluster_custom_object(GROUP, VERSION, PLURAL, name)
        except client.exceptions.ApiException as e:
            if e.status == 404:
                return {}
            raise

    def wait_effective(self, name: str, timeout: float = 90) -> tuple[bool, str]:
        """等待 CR 状态确认注入生效。返回 (success, detail)。"""
        deadline = time.time() + timeout
        last = ""
        while time.time() < deadline:
            cr = self.status(name)
            statuses = (cr.get("status") or {}).get("expStatuses") or []
            if statuses:
                st = statuses[0]
                last = f"state={st.get('state')} success={st.get('success')} error={st.get('error', '')}"
                if st.get("state") in ("Success", "Running") and st.get("success", False):
                    return True, last
                if st.get("state") == "Error":
                    return False, last
            time.sleep(3)
        return False, last or "timeout waiting expStatuses"

    def destroy(self, name: str, timeout: float = 60) -> bool:
        try:
            self.custom.delete_cluster_custom_object(GROUP, VERSION, PLURAL, name)
        except client.exceptions.ApiException as e:
            if e.status != 404:
                raise
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.status(name):
                return True
            time.sleep(2)
        return not self.status(name)

    def cleanup_all(self) -> list[str]:
        """紧急清理：删除所有 cnaa- 前缀的实验 CR。"""
        removed = []
        try:
            lst = self.custom.list_cluster_custom_object(GROUP, VERSION, PLURAL)
            for item in lst.get("items", []):
                n = item["metadata"]["name"]
                if n.startswith("cnaa-"):
                    self.destroy(n)
                    removed.append(n)
        except Exception:
            pass
        return removed