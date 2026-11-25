"""ChaosBlade JVM Executor 适配器（方案 §4.5，代码级故障注入）。

对 Java 服务（如 adservice）注入方法级延迟/自定义异常，进入应用内部
验证超时、异常恢复等容错路径。复用 ChaosBlade CRD 机制（scope=pod,
target=jvm），要求目标 Pod 已 attach chaosblade java agent。

动作映射（固定，不接受任意命令）：
  JVM_DELAY      -> jvm delay（classname/methodname/time）
  JVM_EXCEPTION  -> jvm throwCustomException（classname/methodname/exception）
"""
from __future__ import annotations

import uuid

from common.config import Config
from experiment_runtime.chaosblade_adapter import ChaosBladeAdapter

JVM_ACTIONS = ("JVM_DELAY", "JVM_EXCEPTION")


class JvmInjectionAdapter(ChaosBladeAdapter):
    def __init__(self, cfg: Config):
        super().__init__(cfg)

    def build_experiment(self, action_type: str, namespace: str, labels: dict,
                         params: dict) -> dict:
        """构造 JVM 实验 spec（固定映射，参数白名单）。"""
        label_strs = [f"{k}={v}" for k, v in labels.items()]
        base = [{"name": "namespace", "value": [namespace]},
                {"name": "labels", "value": label_strs}]
        classname = params.get("classname", "")
        methodname = params.get("methodname", "")
        if not classname or not methodname:
            raise ValueError("JVM fault requires classname and methodname params")
        m = base + [{"name": "classname", "value": [classname]},
                    {"name": "methodname", "value": [methodname]}]
        if params.get("process"):
            m.append({"name": "process", "value": [params["process"]]})
        # 限制影响范围：默认仅生效一次
        m.append({"name": "effect-count", "value": [str(params.get("effect_count", 1))]})
        if action_type == "JVM_DELAY":
            m.append({"name": "time", "value": [str(params.get("time_ms", 3000))]})
            return {"scope": "pod", "target": "jvm", "action": "delay",
                    "desc": "cnaa controlled jvm method delay", "matchers": m}
        if action_type == "JVM_EXCEPTION":
            m.append({"name": "exception",
                      "value": [params.get("exception", "java.lang.RuntimeException")]})
            m.append({"name": "exception-message",
                      "value": [params.get("exception_message", "cnaa controlled fault")]})
            return {"scope": "pod", "target": "jvm", "action": "throwCustomException",
                    "desc": "cnaa controlled jvm method exception", "matchers": m}
        raise ValueError(f"unsupported jvm action: {action_type}")

    def inject(self, action_type: str, namespace: str, labels: dict,
               params: dict, service_port: int | None = None) -> str:
        if action_type not in JVM_ACTIONS:
            raise ValueError(f"JvmInjectionAdapter only handles {JVM_ACTIONS}")
        name = f"cnaa-{action_type.lower().replace('_', '-')}-{uuid.uuid4().hex[:8]}"
        exp = self.build_experiment(action_type, namespace, labels, params)
        self.custom.create_cluster_custom_object(
            "chaosblade.io", "v1alpha1", "chaosblades", self._body(name, [exp]))
        return name