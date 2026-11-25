# 独立静态基线 Conftest 策略（§9.3）：通用 Kubernetes 卫生检查，
# deny 返回结构化对象 {contract, target, msg}，由 runner 直接消费。
package main

import future.keywords.contains
import future.keywords.if

# RES-004：副本数 < 2，单 Pod 故障即服务中断
deny contains result if {
	input.kind == "Deployment"
	input.spec.replicas < 2
	result := {
		"contract": "RES-004",
		"target": input.metadata.name,
		"msg": sprintf("replicas=%d < 2 (single pod failure interrupts service)", [input.spec.replicas]),
	}
}

# RES-004：容器缺少 readinessProbe
deny contains result if {
	input.kind == "Deployment"
	c := input.spec.template.spec.containers[_]
	not c.readinessProbe
	result := {
		"contract": "RES-004",
		"target": input.metadata.name,
		"msg": sprintf("container %s has no readinessProbe", [c.name]),
	}
}

# RES-004：容器缺少 livenessProbe
deny contains result if {
	input.kind == "Deployment"
	c := input.spec.template.spec.containers[_]
	not c.livenessProbe
	result := {
		"contract": "RES-004",
		"target": input.metadata.name,
		"msg": sprintf("container %s has no livenessProbe", [c.name]),
	}
}

# SCA-002：容器缺少 resources.limits
deny contains result if {
	input.kind == "Deployment"
	c := input.spec.template.spec.containers[_]
	not c.resources.limits
	result := {
		"contract": "SCA-002",
		"target": input.metadata.name,
		"msg": sprintf("container %s has no resources.limits", [c.name]),
	}
}

# DEP-001：镜像使用 latest 或无 tag（滚动更新/回滚不可复现）
deny contains result if {
	input.kind == "Deployment"
	c := input.spec.template.spec.containers[_]
	endswith(c.image, ":latest")
	result := {
		"contract": "DEP-001",
		"target": input.metadata.name,
		"msg": sprintf("container %s uses :latest tag", [c.name]),
	}
}

deny contains result if {
	input.kind == "Deployment"
	c := input.spec.template.spec.containers[_]
	not contains(c.image, ":")
	result := {
		"contract": "DEP-001",
		"target": input.metadata.name,
		"msg": sprintf("container %s image has no tag", [c.name]),
	}
}