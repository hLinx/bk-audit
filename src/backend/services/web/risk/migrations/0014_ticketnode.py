# Generated by Django 3.2.18 on 2023-09-15 04:43

from django.db import migrations, models

import core.models


class Migration(migrations.Migration):

    dependencies = [
        ("risk", "0013_auto_20230915_1238"),
    ]

    operations = [
        migrations.CreateModel(
            name="TicketNode",
            fields=[
                (
                    "id",
                    core.models.UUIDField(
                        default=core.models.UUIDField.get_default_value,
                        max_length=64,
                        primary_key=True,
                        serialize=False,
                        verbose_name="ID",
                    ),
                ),
                ("risk_id", models.CharField(db_index=True, max_length=255, verbose_name="Risk ID")),
                ("operator", models.CharField(db_index=True, max_length=255, verbose_name="Operator")),
                (
                    "current_operator",
                    models.JSONField(blank=True, default=list, null=True, verbose_name="Current Operator"),
                ),
                ("action", models.CharField(db_index=True, max_length=64, verbose_name="Action")),
                ("timestamp", models.FloatField(db_index=True, verbose_name="Timestamp")),
                ("time", models.CharField(max_length=32, verbose_name="Time")),
                ("process_result", models.JSONField(default=dict, verbose_name="Process Result")),
                ("extra", models.JSONField(default=dict, verbose_name="Extra")),
                (
                    "status",
                    models.CharField(
                        choices=[("running", "运行中"), ("finished", "已完成")],
                        db_index=True,
                        default="running",
                        max_length=32,
                        verbose_name="Status",
                    ),
                ),
            ],
            options={
                "verbose_name": "Ticket History",
                "verbose_name_plural": "Ticket History",
                "ordering": ["-timestamp"],
            },
        ),
    ]