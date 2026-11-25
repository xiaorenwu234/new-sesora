# 主系统 Conftest 策略（deployability/scalability 契约的确定性证据）。
# 与静态基线策略独立维护；deny 返回 {contract, target, msg} 结构化对象。
package main

import future.keywords.contains
import future.keywords.if

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

# SCA-002：容器缺少 resources.requests
deny contains result if {
	input.kind == "Deployment"
	c := input.spec.template.spec.containers[_]
	not c.resources.requests
	result := {
		"contract": "SCA-002",
		"target": input.metadata.name,
		"msg": sprintf("container %s has no resources.requests", [c.name]),
	}
}

# DEP-002：优雅终止窗口过短（< 5s）
deny contains result if {
	input.kind == "Deployment"
	input.spec.template.spec.terminationGracePeriodSeconds < 5
	result := {
		"contract": "DEP-002",
		"target": input.metadata.name,
		"msg": sprintf("terminationGracePeriodSeconds=%d too short for graceful drain",
			[input.spec.template.spec.terminationGracePeriodSeconds]),
	}
}

# DEP-001：镜像 latest tag（滚动更新不可复现）
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

# DEP-001：镜像无 tag（回滚不可复现；构建期注入 tag 属部署时关注点）
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

# RES-004：容器缺少 readinessProbe（滚动更新无就绪门控）
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

# RES-004：容器缺少 livenessProbe（故障 Pod 无法自愈）
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