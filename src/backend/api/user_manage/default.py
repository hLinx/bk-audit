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
import base64
import os

from bk_resource import BkApiResource
from django.conf import settings
from django.utils.translation import gettext_lazy

from api.domains import USER_MANAGE_URL


class UserManageResource(BkApiResource, abc.ABC):
    base_url = USER_MANAGE_URL
    module_name = "user_manage"


class ListUsers(UserManageResource):
    name = gettext_lazy("获取用户列表")
    method = "GET"
    action = "/list_users/"


class RetrieveUser(UserManageResource):
    name = gettext_lazy("获取单个用户信息")
    method = "GET"
    action = "/retrieve_user/"


class GetSnapshotSchema(UserManageResource):
    name = gettext_lazy("获取用户快照Schema")
    method = "POST"
    base_url = settings.SNAPSHOT_USERINFO_RESOURCE_URL
    action = "/"

    def build_header(self, validated_request_data):
        headers = super().build_header(validated_request_data)
        token = "Basic {token}".format(
            token=base64.b64encode(settings.SNAPSHOT_USERINFO_RESOURCE_TOKEN.encode()).decode()
        )
        headers.update({"Authorization": token})
        return headers

    def perform_request(self, validated_request_data):
        params = {
            "method": "fetch_resource_type_schema",
            "type": os.getenv("BKAPP_SENSITIVE_USER_DATA_RESOURCE_ID", "user"),
        }
        return super().perform_request(params)

    def parse_response(self, response):
        data = super().parse_response(response)
        return data.get("properties", {})
