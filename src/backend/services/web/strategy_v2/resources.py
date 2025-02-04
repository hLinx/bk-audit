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
import datetime
from collections import defaultdict
from typing import List

from bk_resource import Resource, api, resource
from bk_resource.exceptions import APIRequestError
from blueapps.utils.request_provider import get_local_request, get_request_username
from django.db import transaction
from django.db.models import Count, Q
from django.shortcuts import get_object_or_404
from django.utils.translation import gettext, gettext_lazy
from pypinyin import lazy_pinyin
from rest_framework.settings import api_settings

from apps.audit.client import bk_audit_client
from apps.meta.models import DataMap, Tag
from apps.meta.utils.fields import (
    DIMENSION_FIELD_TYPES,
    EXTEND_DATA,
    FILED_DISPLAY_NAME_ALIAS_KEY,
    PYTHON_TO_ES,
    SNAPSHOT_USER_INFO,
    SNAPSHOT_USER_INFO_HIDE_FIELDS,
    STRATEGY_DISPLAY_FIELDS,
)
from apps.permission.handlers.actions import ActionEnum
from apps.permission.handlers.drf import ActionPermission
from apps.permission.handlers.permission import Permission
from apps.permission.handlers.resource_types import ResourceEnum
from core.utils.tools import choices_to_dict
from services.web.analyze.constants import (
    ControlTypeChoices,
    FilterOperator,
    OffsetUnit,
)
from services.web.analyze.controls.base import Controller
from services.web.analyze.exceptions import ControlNotExist
from services.web.analyze.models import Control
from services.web.analyze.tasks import call_controller
from services.web.strategy_v2.constants import (
    HAS_UPDATE_TAG_ID,
    HAS_UPDATE_TAG_NAME,
    MappingType,
    StrategyAlgorithmOperator,
    StrategyOperator,
    StrategyStatusChoices,
    TableType,
)
from services.web.strategy_v2.exceptions import ControlChangeError, StrategyPendingError
from services.web.strategy_v2.models import Strategy, StrategyAuditInstance, StrategyTag
from services.web.strategy_v2.serializers import (
    AIOPSConfigSerializer,
    BKMStrategySerializer,
    CreateStrategyRequestSerializer,
    CreateStrategyResponseSerializer,
    GetRTFieldsRequestSerializer,
    GetRTFieldsResponseSerializer,
    GetStrategyCommonResponseSerializer,
    GetStrategyFieldValueRequestSerializer,
    GetStrategyFieldValueResponseSerializer,
    GetStrategyStatusRequestSerializer,
    ListStrategyFieldsRequestSerializer,
    ListStrategyFieldsResponseSerializer,
    ListStrategyRequestSerializer,
    ListStrategyResponseSerializer,
    ListStrategyTagsResponseSerializer,
    ListTablesRequestSerializer,
    RetryStrategyRequestSerializer,
    StrategyInfoSerializer,
    ToggleStrategyRequestSerializer,
    UpdateStrategyRequestSerializer,
    UpdateStrategyResponseSerializer,
)
from services.web.strategy_v2.utils.field_value import FieldValueHandler
from services.web.strategy_v2.utils.table import TableHandler


class StrategyV2Base(Resource, abc.ABC):
    tags = ["StrategyV2"]

    def _save_tags(self, strategy_id: int, tag_names: list) -> None:
        StrategyTag.objects.filter(strategy_id=strategy_id).delete()
        if not tag_names:
            return
        tags = resource.meta.save_tags([{"tag_name": t} for t in tag_names])
        StrategyTag.objects.bulk_create([StrategyTag(strategy_id=strategy_id, tag_id=t["tag_id"]) for t in tags])

    def _validate_configs(self, validated_request_data: dict) -> None:
        control_type_id = Control.objects.get(control_id=validated_request_data["control_id"]).control_type_id
        if control_type_id == ControlTypeChoices.BKM.value:
            configs_serializer_class = BKMStrategySerializer
        elif control_type_id == ControlTypeChoices.AIOPS.value:
            configs_serializer_class = AIOPSConfigSerializer
        else:
            raise ControlNotExist()
        configs_serializer = configs_serializer_class(data=validated_request_data["configs"])
        configs_serializer.is_valid(raise_exception=True)
        validated_request_data["configs"] = configs_serializer.validated_data


class CreateStrategy(StrategyV2Base):
    name = gettext_lazy("Create Strategy")
    RequestSerializer = CreateStrategyRequestSerializer
    ResponseSerializer = CreateStrategyResponseSerializer

    def perform_request(self, validated_request_data):
        with transaction.atomic():
            # pop tag
            tag_names = validated_request_data.pop("tags", [])
            # check configs
            self._validate_configs(validated_request_data)
            # save strategy
            strategy: Strategy = Strategy.objects.create(**validated_request_data)
            # save strategy tag
            self._save_tags(strategy_id=strategy.strategy_id, tag_names=tag_names)
        # create
        try:
            call_controller(Controller.create.__name__, strategy.strategy_id)
        except Exception as err:
            strategy.status = StrategyStatusChoices.START_FAILED
            strategy.status_msg = str(err)
            strategy.save(update_fields=["status", "status_msg"])
            raise err
        # auth
        username = get_request_username()
        if username:
            resource_instance = ResourceEnum.STRATEGY.create_instance(strategy.strategy_id)
            Permission(username).grant_creator_action(resource_instance)
        # audit
        bk_audit_client.add_event(
            action=ActionEnum.CREATE_STRATEGY,
            resource_type=ResourceEnum.STRATEGY,
            instance=StrategyAuditInstance(strategy),
            event_content=ActionEnum.CREATE_STRATEGY.name,
        )
        # response
        return strategy


class UpdateStrategy(StrategyV2Base):
    name = gettext_lazy("Update Strategy")
    RequestSerializer = UpdateStrategyRequestSerializer
    ResponseSerializer = UpdateStrategyResponseSerializer

    def perform_request(self, validated_request_data):
        with transaction.atomic():
            # pop tag
            tag_names = validated_request_data.pop("tags", [])
            # check configs
            self._validate_configs(validated_request_data)
            # load strategy
            strategy: Strategy = get_object_or_404(
                Strategy, strategy_id=validated_request_data.pop("strategy_id", int())
            )
            instance_origin_data = StrategyInfoSerializer(strategy).data
            # check control
            if strategy.control_id != validated_request_data["control_id"]:
                raise ControlChangeError()
            # check strategy status
            if strategy.status in [
                StrategyStatusChoices.STARTING,
                StrategyStatusChoices.UPDATING,
                StrategyStatusChoices.STOPPING,
            ]:
                raise StrategyPendingError()
            # save strategy
            for key, val in validated_request_data.items():
                setattr(strategy, key, val)
            strategy.save(update_fields=validated_request_data.keys())
            strategy.status = StrategyStatusChoices.UPDATING
            strategy.save(update_fields=["status"])
            # save strategy tag
            self._save_tags(strategy_id=strategy.strategy_id, tag_names=tag_names)
        # update
        try:
            call_controller(Controller.update.__name__, strategy.strategy_id)
        except Exception as err:
            strategy.status = StrategyStatusChoices.UPDATE_FAILED
            strategy.status_msg = str(err)
            strategy.save(update_fields=["status", "status_msg"])
            raise err
        # audit
        setattr(strategy, "instance_origin_data", instance_origin_data)
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_STRATEGY,
            resource_type=ResourceEnum.STRATEGY,
            instance=StrategyAuditInstance(strategy),
            event_content=ActionEnum.EDIT_STRATEGY.name,
        )
        # response
        return strategy


class DeleteStrategy(StrategyV2Base):
    name = gettext_lazy("Delete Strategy")

    @transaction.atomic()
    def perform_request(self, validated_request_data):
        strategy = get_object_or_404(Strategy, strategy_id=validated_request_data["strategy_id"])
        # delete tags
        StrategyTag.objects.filter(strategy_id=validated_request_data["strategy_id"]).delete()
        # delete
        try:
            call_controller(Controller.delete.__name__, validated_request_data["strategy_id"])
        except Exception as err:
            strategy.status = StrategyStatusChoices.DELETE_FAILED
            strategy.status_msg = str(err)
            strategy.save(update_fields=["status", "status_msg"])
            raise err
        # delete strategy
        bk_audit_client.add_event(
            action=ActionEnum.DELETE_STRATEGY,
            resource_type=ResourceEnum.STRATEGY,
            instance=StrategyAuditInstance(strategy),
            event_content=ActionEnum.DELETE_STRATEGY.name,
        )
        strategy.delete()


class ListStrategy(StrategyV2Base):
    name = gettext_lazy("List Strategy")
    RequestSerializer = ListStrategyRequestSerializer
    ResponseSerializer = ListStrategyResponseSerializer
    many_response_data = True

    def perform_request(self, validated_request_data):
        # init queryset
        order_field = validated_request_data.get("order_field") or "-strategy_id"
        queryset = Strategy.objects.filter(namespace=validated_request_data["namespace"]).order_by(order_field)
        # 特殊筛选
        if HAS_UPDATE_TAG_ID in validated_request_data.get("tag", []):
            validated_request_data["tag"] = [t for t in validated_request_data["tag"] if t != HAS_UPDATE_TAG_ID]
            queryset = queryset.filter(
                strategy_id__in=[s.strategy_id for s in resource.strategy_v2.list_has_update_strategy()]
            )
        # tag filter
        if validated_request_data.get("tag"):
            # tag 筛选
            strategy_ids = StrategyTag.objects.filter(tag_id__in=validated_request_data["tag"]).values("strategy_id")
            queryset = queryset.filter(strategy_id__in=strategy_ids)
        # exact filter
        for key in ["strategy_id", "status"]:
            if validated_request_data.get(key):
                queryset = queryset.filter(**{f"{key}__in": validated_request_data[key]})
        # fuzzy filter
        for key in ["strategy_name"]:
            if validated_request_data.get(key):
                q = Q()
                for item in validated_request_data[key]:
                    q |= Q(**{f"{key}__contains": item})
                queryset = queryset.filter(q)
        # add tags
        all_tags = StrategyTag.objects.filter(strategy_id__in=queryset.values("strategy_id"))
        tag_map = defaultdict(list)
        for t in all_tags:
            tag_map[t.strategy_id].append(t.tag_id)
        for item in queryset:
            setattr(item, "tags", tag_map.get(item.strategy_id, []))
        # audit
        bk_audit_client.add_event(
            action=ActionEnum.LIST_STRATEGY,
            resource_type=ResourceEnum.STRATEGY,
            event_content=ActionEnum.LIST_STRATEGY.name,
            extend_data=validated_request_data,
        )
        # response
        return queryset


class ListStrategyAll(StrategyV2Base):
    name = gettext_lazy("List All Strategy")

    def perform_request(self, validated_request_data):
        if not ActionPermission(
            actions=[ActionEnum.LIST_STRATEGY, ActionEnum.LIST_RISK, ActionEnum.EDIT_RISK]
        ).has_permission(request=get_local_request(), view=self):
            return []
        strategies: List[Strategy] = Strategy.objects.all()
        data = [{"label": s.strategy_name, "value": s.strategy_id} for s in strategies]
        data.sort(key=lambda s: s["label"])
        return data


class ToggleStrategy(StrategyV2Base):
    name = gettext_lazy("Toggle Strategy")
    RequestSerializer = ToggleStrategyRequestSerializer

    def perform_request(self, validated_request_data):
        strategy = get_object_or_404(Strategy, strategy_id=validated_request_data["strategy_id"])
        bk_audit_client.add_event(
            action=ActionEnum.EDIT_STRATEGY,
            resource_type=ResourceEnum.STRATEGY,
            instance=StrategyAuditInstance(strategy),
            event_content=ActionEnum.EDIT_STRATEGY.name,
            extend_data=validated_request_data,
        )
        if validated_request_data["toggle"]:
            call_controller(Controller.enable.__name__, strategy.strategy_id)
            return
        call_controller(Controller.disabled.__name__, strategy.strategy_id)


class RetryStrategy(StrategyV2Base):
    name = gettext_lazy("Retry Strategy")
    RequestSerializer = RetryStrategyRequestSerializer

    def perform_request(self, validated_request_data):
        # load strategy
        strategy = get_object_or_404(Strategy, strategy_id=validated_request_data["strategy_id"])
        # try update
        try:
            if strategy.backend_data and (strategy.backend_data.get("id") or strategy.backend_data.get("flow_id")):
                call_controller(Controller.update.__name__, strategy.strategy_id)
            else:
                call_controller(Controller.create.__name__, strategy.strategy_id)
        except Exception as err:
            strategy.status = StrategyStatusChoices.FAILED
            strategy.status_msg = str(err)
            strategy.save(update_fields=["status", "status_msg"])
            raise err


class ListHasUpdateStrategy(StrategyV2Base):
    name = gettext_lazy("List Has Update Strategy")

    def perform_request(self, validated_request_data):
        # load last control versions
        all_controls = resource.analyze.control()
        controls = {}
        # 获取每个版本的最新版本
        for cv in all_controls:
            if cv["control_id"] not in controls.keys():
                controls[cv["control_id"]] = cv["versions"][0]["control_version"]
        # 获取所有策略
        all_strategies = Strategy.objects.all()
        # 判断需要更新的数量
        has_update = []
        for s in all_strategies:
            if controls.get(s.control_id, s.control_version) > s.control_version:
                has_update.append(s)
        return has_update


class ListStrategyTags(StrategyV2Base):
    name = gettext_lazy("List Strategy Tags")
    ResponseSerializer = ListStrategyTagsResponseSerializer
    many_response_data = True

    def perform_request(self, validated_request_data):
        # load all tags
        tag_count = list(StrategyTag.objects.all().values("tag_id").annotate(strategy_count=Count("tag_id")).order_by())
        tag_map = {t.tag_id: {"name": t.tag_name} for t in Tag.objects.all()}
        for t in tag_count:
            t.update({"tag_name": tag_map.get(t["tag_id"], {}).get("name", t["tag_id"])})
        # sort
        tag_count.sort(key=lambda tag: [lazy_pinyin(tag["tag_name"].lower(), errors="ignore"), tag["tag_name"].lower()])
        # add has update
        tag_count = [
            {
                "tag_name": str(HAS_UPDATE_TAG_NAME),
                "tag_id": HAS_UPDATE_TAG_ID,
                "strategy_count": len(resource.strategy_v2.list_has_update_strategy()),
            }
        ] + tag_count
        # response
        return tag_count


class ListStrategyFields(StrategyV2Base):
    name = gettext_lazy("List Strategy Fields")
    RequestSerializer = ListStrategyFieldsRequestSerializer
    ResponseSerializer = ListStrategyFieldsResponseSerializer
    many_response_data = True

    def perform_request(self, validated_request_data):
        # load log field
        system_id = validated_request_data.get("system_id")
        action_id = validated_request_data.get("action_id")
        if system_id and action_id:
            data = self.load_action_fields(validated_request_data["namespace"], system_id, action_id)
        else:
            data = self.load_public_fields()
        # sort and response
        data.sort(key=lambda field: (field["priority_index"], field["field_name"]), reverse=True)
        return data

    def load_action_fields(self, namespace: str, system_id: str, action_id: str) -> List[dict]:
        data = []
        end_time = datetime.datetime.now()
        start_time = end_time - datetime.timedelta(days=7)
        logs = resource.esquery.search_all(
            namespace=namespace,
            start_time=start_time.strftime(api_settings.DATETIME_FORMAT),
            end_time=end_time.strftime(api_settings.DATETIME_FORMAT),
            query_string="*",
            sort_list="",
            page=1,
            page_size=1,
            bind_system_info=False,
            system_id=system_id,
            action_id=action_id,
        )
        if logs.get("results", []):
            for key, _ in logs["results"][0].get("extend_data", {}).items():
                data.append(
                    {
                        "field_name": f"{EXTEND_DATA.field_name}.{key}",
                        "description": f"{str(EXTEND_DATA.description)}.{key}",
                        "field_type": PYTHON_TO_ES.get(type(key), type(key)),
                        "priority_index": EXTEND_DATA.priority_index,
                        "is_dimension": False,
                    }
                )
        return data

    def load_public_fields(self) -> List[dict]:
        # load exist fields
        data = [
            {
                "field_name": field.field_name,
                "description": f"{str(field.description)}({field.field_name})",
                "field_type": field.field_type,
                "priority_index": field.priority_index,
                "is_dimension": field.field_type in DIMENSION_FIELD_TYPES,
            }
            for field in STRATEGY_DISPLAY_FIELDS
        ]
        # load snapshot user info fields
        try:
            schema = api.user_manage.get_snapshot_schema()
        except APIRequestError:
            schema = {}
        for field_name, field_data in schema.items():
            if field_name in SNAPSHOT_USER_INFO_HIDE_FIELDS:
                continue
            data.append(
                {
                    "field_name": "{}.{}".format(SNAPSHOT_USER_INFO.field_name, field_name),
                    "description": "{}.{}({})".format(
                        gettext(
                            DataMap.get_alias(
                                FILED_DISPLAY_NAME_ALIAS_KEY,
                                SNAPSHOT_USER_INFO.field_name,
                                default=SNAPSHOT_USER_INFO.description,
                            )
                        ),
                        gettext(field_data.get("description", field_name)),
                        field_name,
                    ),
                    "field_type": field_data.get("type", ""),
                    "priority_index": SNAPSHOT_USER_INFO.priority_index,
                    "is_dimension": True,
                }
            )
        return data


class GetStrategyFieldValue(StrategyV2Base):
    name = gettext_lazy("Get Strategy Field Value")
    RequestSerializer = GetStrategyFieldValueRequestSerializer
    ResponseSerializer = GetStrategyFieldValueResponseSerializer
    many_response_data = True

    def perform_request(self, validated_request_data):
        handler = FieldValueHandler(
            field_name=validated_request_data["field_name"],
            namespace=validated_request_data["namespace"],
            system_id=validated_request_data.get("system_id"),
        )
        return handler.values


class GetStrategyCommon(StrategyV2Base):
    name = gettext_lazy("Get Strategy Common")
    ResponseSerializer = GetStrategyCommonResponseSerializer

    def perform_request(self, validated_request_data):
        return {
            "strategy_operator": choices_to_dict(StrategyOperator, val="value", name="label"),
            "filter_operator": choices_to_dict(FilterOperator, val="value", name="label"),
            "algorithm_operator": choices_to_dict(StrategyAlgorithmOperator, val="value", name="label"),
            "table_type": [
                {"value": value, "label": str(label), "config": TableType.get_config(value)}
                for value, label in TableType.choices
            ],
            "strategy_status": choices_to_dict(StrategyStatusChoices, val="value", name="label"),
            "offset_unit": choices_to_dict(OffsetUnit, val="value", name="label"),
            "mapping_type": choices_to_dict(MappingType, val="value", name="label"),
        }


class ListTables(StrategyV2Base):
    name = gettext_lazy("List Tables")
    RequestSerializer = ListTablesRequestSerializer

    def perform_request(self, validated_request_data):
        return TableHandler(**validated_request_data).list_tables()


class GetRTFields(StrategyV2Base):
    name = gettext_lazy("Get RT Fields")
    RequestSerializer = GetRTFieldsRequestSerializer
    ResponseSerializer = GetRTFieldsResponseSerializer
    many_response_data = True

    def perform_request(self, validated_request_data):
        fields = api.bk_base.get_rt_fields(result_table_id=validated_request_data["table_id"])
        return [
            {
                "label": "{}({})".format(field["field_alias"] or field["field_name"], field["field_name"]),
                "value": field["field_name"],
                "field_type": field["field_type"],
            }
            for field in fields
        ]


class GetStrategyStatus(StrategyV2Base):
    name = gettext_lazy("Get Strategy Status")
    RequestSerializer = GetStrategyStatusRequestSerializer

    def perform_request(self, validated_request_data):
        bk_audit_client.add_event(
            action=ActionEnum.LIST_STRATEGY,
            resource_type=ResourceEnum.STRATEGY,
            event_content=ActionEnum.LIST_STRATEGY.name,
            extend_data=validated_request_data,
        )
        return {
            s.strategy_id: {"status": s.status, "status_msg": s.status_msg}
            for s in Strategy.objects.filter(strategy_id__in=validated_request_data["strategy_ids"])
        }
