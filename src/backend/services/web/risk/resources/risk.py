# -*- coding: utf-8 -*-
"""
TencentBlueKing is pleased to support the open source community by making
蓝鲸智云 - 审计中心 (BlueKing - Audit Center) available.
Copyright (C) 2023 THL A29 Limited,
a Tencent company. All rights reserved.
Licensed under the MIT License (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at http://opensource.org/licenses/MIT
Unless required by applicable law or agreed to in writing,
software distributed under the License is distributed on
an "AS IS" BASIS, WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND,
either express or implied. See the License for the
specific language governing permissions and limitations under the License.
We undertake not to change the open source license (MIT license) applicable
to the current version of the project delivered to anyone in the future.
"""

import abc

from bk_resource import Resource, api
from bk_resource.settings import bk_resource_settings
from blueapps.utils.request_provider import get_request_username
from django.conf import settings
from django.db import transaction
from django.db.models import Count, Q, QuerySet
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext, gettext_lazy

from apps.audit.client import bk_audit_client
from apps.itsm.constants import TicketOperate, TicketStatus
from apps.meta.models import Tag
from apps.permission.handlers.actions import ActionEnum
from apps.permission.handlers.drf import wrapper_permission_field
from apps.permission.handlers.resource_types import ResourceEnum
from apps.sops.constants import SOPSTaskOperation, SOPSTaskStatus
from core.exceptions import RiskStatusInvalid
from core.utils.tools import choices_to_dict
from services.web.risk.constants import (
    RISK_SHOW_FIELDS,
    RiskLabel,
    RiskStatus,
    TicketNodeStatus,
)
from services.web.risk.handlers.ticket import (
    AutoProcess,
    CloseRisk,
    CustomProcess,
    ForApprove,
    MisReport,
    ReOpen,
    ReOpenMisReport,
    TransOperator,
)
from services.web.risk.models import (
    ProcessApplication,
    Risk,
    RiskAuditInstance,
    RiskExperience,
    TicketNode,
)
from services.web.risk.serializers import (
    CustomAutoProcessReqSerializer,
    CustomCloseRiskRequestSerializer,
    CustomTransRiskReqSerializer,
    ForceRevokeApproveTicketReqSerializer,
    ForceRevokeAutoProcessReqSerializer,
    ListRiskRequestSerializer,
    ListRiskResponseSerializer,
    ReopenRiskReqSerializer,
    RetryAutoProcessReqSerializer,
    RiskInfoSerializer,
    TicketNodeSerializer,
    UpdateRiskLabelReqSerializer,
)
from services.web.risk.tasks import sync_auto_result


class RiskMeta(Resource, abc.ABC):
    tags = ["Risk"]


class RetrieveRisk(RiskMeta):
    name = gettext_lazy("获取风险详情")

    def perform_request(self, validated_request_data):
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        data = RiskInfoSerializer(risk).data
        data = wrapper_permission_field(
            data, actions=[ActionEnum.EDIT_RISK], id_field=lambda risk: risk["risk_id"], many=False
        )
        bk_audit_client.add_event(
            action=ActionEnum.LIST_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )
        risk = data[0]
        nodes = TicketNode.objects.filter(risk_id=risk["risk_id"]).order_by("timestamp")
        risk["ticket_history"] = TicketNodeSerializer(nodes, many=True).data
        return risk


class ListRisk(RiskMeta):
    name = gettext_lazy("获取风险列表")
    RequestSerializer = ListRiskRequestSerializer
    ResponseSerializer = ListRiskResponseSerializer
    many_response_data = True

    def perform_request(self, validated_request_data):
        order_field = validated_request_data.pop("order_field", "-last_operate_time")
        risks = self.load_risks(validated_request_data).order_by(order_field)
        # 获取关联的经验
        experiences = {
            e["risk_id"]: e["count"]
            for e in RiskExperience.objects.filter(risk_id__in=risks.values("risk_id"))
            .values("risk_id")
            .order_by("risk_id")
            .annotate(count=Count("risk_id"))
        }
        for risk in risks:
            setattr(risk, "experiences", experiences.get(risk.risk_id, 0))
        # 响应
        return risks

    def load_risks(self, validated_request_data: dict) -> QuerySet:
        # 构造表达式
        q = Q()
        for key, val in validated_request_data.items():
            if not val:
                continue
            _q = Q()
            for i in val:
                _q |= Q(**{key: i})
            q &= _q
        # 获取有权限且符合表达式的
        risks = Risk.load_authed_risks(action=ActionEnum.LIST_RISK).filter(q)
        bk_audit_client.add_event(
            action=ActionEnum.LIST_RISK, resource_type=ResourceEnum.RISK, extend_data=validated_request_data
        )
        return risks


class ListMineRisk(ListRisk):
    name = gettext_lazy("获取待我处理的风险列表")

    def load_risks(self, validated_request_data):
        queryset = super().load_risks(validated_request_data)
        queryset = queryset.filter(current_operator__contains=get_request_username())
        return queryset


class ListRiskFields(RiskMeta):
    name = gettext_lazy("获取风险字段")

    def perform_request(self, validated_request_data):
        return [{"id": f.name, "name": str(f.verbose_name)} for f in Risk._meta.fields if f.name in RISK_SHOW_FIELDS]


class UpdateRiskLabel(RiskMeta):
    name = gettext_lazy("更新风险标记")
    RequestSerializer = UpdateRiskLabelReqSerializer
    ResponseSerializer = ListRiskResponseSerializer

    @transaction.atomic()
    def perform_request(self, validated_request_data):
        # 初始化风险
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        # 确认要变更的类型
        new_risk_label = validated_request_data["risk_label"]
        # 重开需要登记重开，并转单
        if new_risk_label == RiskLabel.NORMAL:
            ReOpenMisReport(risk_id=risk.risk_id, operator=get_request_username()).run(
                new_operators=validated_request_data.get("new_operators", [])
            )
        # 误报需要登记误报，并关单
        elif new_risk_label == RiskLabel.MISREPORT:
            MisReport(risk_id=risk.risk_id, operator=get_request_username()).run(
                description=validated_request_data["description"],
                revoke_process=validated_request_data["revoke_process"],
            )
        risk.refresh_from_db()
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )
        return risk


class RiskStatusCommon(RiskMeta):
    name = gettext_lazy("获取风险状态类型")

    def perform_request(self, validated_request_data):
        return choices_to_dict(RiskStatus)


class ListRiskTags(RiskMeta):
    name = gettext_lazy("获取风险的标签")

    def perform_request(self, validated_request_data):
        return [{"id": t.tag_id, "name": t.tag_name} for t in Tag.objects.all()]


class CustomCloseRisk(RiskMeta):
    name = gettext_lazy("人工关单")
    RequestSerializer = CustomCloseRiskRequestSerializer

    @transaction.atomic()
    def perform_request(self, validated_request_data):
        username = get_request_username()
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        CustomProcess(risk_id=risk.risk_id, operator=username).run(
            custom_action=CloseRisk.__name__, description=validated_request_data["description"]
        )
        CloseRisk(risk_id=risk.risk_id, operator=username).run(description=gettext("%s 人工关单") % username)
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )


class CustomTransRisk(RiskMeta):
    name = gettext_lazy("人工转单")
    RequestSerializer = CustomTransRiskReqSerializer

    @transaction.atomic()
    def perform_request(self, validated_request_data):
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        TransOperator(risk_id=risk.risk_id, operator=get_request_username()).run(
            new_operators=validated_request_data["new_operators"], description=validated_request_data["description"]
        )
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )


class CustomAutoProcess(RiskMeta):
    name = gettext_lazy("人工执行处理套餐")
    RequestSerializer = CustomAutoProcessReqSerializer

    @transaction.atomic()
    def perform_request(self, validated_request_data):
        username = get_request_username()
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        # 获取处理套餐
        pa = get_object_or_404(ProcessApplication, id=validated_request_data["pa_id"])
        # 记录执行
        CustomProcess(risk_id=risk.risk_id, operator=username).run(
            custom_action=AutoProcess.__name__,
            pa_id=validated_request_data["pa_id"],
            pa_params=validated_request_data["pa_params"],
            auto_close_risk=validated_request_data["auto_close_risk"],
        )
        # 处理节点
        if pa.need_approve:
            processor = ForApprove
        else:
            processor = AutoProcess
        processor(risk_id=risk.risk_id, operator=username).run(
            pa_config={
                "pa_id": validated_request_data["pa_id"],
                "pa_params": validated_request_data["pa_params"],
                "auto_close_risk": validated_request_data["auto_close_risk"],
            }
        )
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )


class ForceRevokeApproveTicket(RiskMeta):
    name = gettext_lazy("强制撤销审批单据")
    RequestSerializer = ForceRevokeApproveTicketReqSerializer

    def perform_request(self, validated_request_data):
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        node = get_object_or_404(TicketNode, risk_id=risk.risk_id, id=validated_request_data["node_id"])
        if node.action != ForApprove.__name__:
            raise RiskStatusInvalid(message=gettext("节点类型异常 => %s") % node.action)
        sn = node.process_result["ticket"]["sn"]
        # 判断单据状态
        status = api.bk_itsm.ticket_approve_result(sn=[sn])[0]
        if status["current_status"] in TicketStatus.get_finished_status():
            sync_auto_result(node_id=node.id)
            return
        # 关单
        api.bk_itsm.operate_ticket(
            sn=sn,
            operator=bk_resource_settings.PLATFORM_AUTH_ACCESS_USERNAME,
            action_type=TicketOperate.WITHDRAW,
            action_message=str(TicketOperate.WITHDRAW.label),
        )
        sync_auto_result(node_id=node.id)
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )


class ForceRevokeAutoProcess(RiskMeta):
    name = gettext_lazy("强制终止处理套餐")
    RequestSerializer = ForceRevokeAutoProcessReqSerializer

    def perform_request(self, validated_request_data):
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        node = get_object_or_404(TicketNode, risk_id=risk.risk_id, id=validated_request_data["node_id"])
        if node.action != AutoProcess.__name__:
            raise RiskStatusInvalid(message=gettext("节点类型异常 => %s") % node.action)
        task_id = node.process_result["task"]["task_id"]
        # 判断套餐状态
        status = api.bk_sops.get_task_status(task_id=task_id, bk_biz_id=settings.DEFAULT_BK_BIZ_ID)
        if status["state"] in SOPSTaskStatus.get_finished_status():
            sync_auto_result(node_id=node.id)
            return
        # 终止套餐
        api.bk_sops.operate_task(bk_biz_id=settings.DEFAULT_BK_BIZ_ID, task_id=task_id, action=SOPSTaskOperation.REVOKE)
        sync_auto_result(node_id=node.id)
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )


class RetryAutoProcess(RiskMeta):
    name = gettext_lazy("重试处理套餐")
    RequestSerializer = RetryAutoProcessReqSerializer

    def perform_request(self, validated_request_data):
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        node = get_object_or_404(TicketNode, risk_id=risk.risk_id, id=validated_request_data["node_id"])
        task_id = node.process_result.get("task", {}).get("task_id", "")
        # 获取节点状态
        status = api.bk_sops.get_task_status(task_id=task_id, bk_biz_id=settings.DEFAULT_BK_BIZ_ID)
        # 对每个失败的节点，获取节点输入并重试
        for item in status["children"].values():
            if item["state"] != SOPSTaskStatus.FAILED:
                continue
            io_data = api.bk_sops.get_node_data(
                task_id=task_id, bk_biz_id=settings.DEFAULT_BK_BIZ_ID, node_id=item["id"]
            )
            api.bk_sops.operate_node(
                task_id=task_id,
                bk_biz_id=settings.DEFAULT_BK_BIZ_ID,
                node_id=item["id"],
                action=SOPSTaskOperation.RETRY,
                inputs=io_data["inputs"],
            )
        with transaction.atomic():
            node.status = TicketNodeStatus.RUNNING
            node.process_result["status"]["state"] = SOPSTaskStatus.RUNNING
            node.save(update_fields=["status", "process_result"])
            # 若最后一个节点为处理套餐节点，则更新状态为自动处理中
            if risk.last_history.id == node.id:
                risk.status = RiskStatus.AUTO_PROCESS
                risk.save(update_fields=["status"])
        # 更新节点信息
        sync_auto_result.apply_async(countdown=60, kwargs={"node_id": node.id})


class ReopenRisk(RiskMeta):
    name = gettext_lazy("重开单据")
    RequestSerializer = ReopenRiskReqSerializer

    def perform_request(self, validated_request_data):
        risk = get_object_or_404(Risk, risk_id=validated_request_data["risk_id"])
        origin_data = RiskInfoSerializer(risk).data
        ReOpen(risk_id=risk.risk_id, operator=get_request_username()).run(
            new_operators=validated_request_data["new_operators"],
            description=gettext("%s 重开单据，指定处理人 %s")
            % (get_request_username(), ";".join(validated_request_data["new_operators"])),
        )
        setattr(risk, "instance_origin_data", origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_RISK,
            resource_type=ResourceEnum.RISK,
            instance=RiskAuditInstance(risk),
            extend_data=validated_request_data,
        )
