# Generated by Django 3.2.12 on 2023-03-20 12:42

import django.utils.timezone
from django.db import migrations, models


class Migration(migrations.Migration):

    initial = True

    dependencies = []

    operations = [
        migrations.CreateModel(
            name="NoticeGroup",
            fields=[
                ("created_at", models.DateTimeField(default=django.utils.timezone.now, verbose_name="创建时间")),
                ("created_by", models.CharField(default="", max_length=32, verbose_name="创建者")),
                ("updated_at", models.DateTimeField(blank=True, null=True, verbose_name="更新时间")),
                ("updated_by", models.CharField(blank=True, default="", max_length=32, verbose_name="修改者")),
                ("is_deleted", models.BooleanField(default=False, verbose_name="是否删除")),
                ("group_id", models.BigAutoField(primary_key=True, serialize=False, verbose_name="通知组ID")),
                ("group_name", models.CharField(db_index=True, max_length=64, verbose_name="通知组名称")),
                ("group_member", models.JSONField(default=list, verbose_name="通知组成员")),
                ("notice_config", models.JSONField(default=list, verbose_name="通知配置")),
                ("description", models.TextField(blank=True, null=True, verbose_name="描述")),
            ],
            options={
                "verbose_name": "通知组",
                "verbose_name_plural": "通知组",
                "ordering": ["-group_id"],
            },
        ),
    ]