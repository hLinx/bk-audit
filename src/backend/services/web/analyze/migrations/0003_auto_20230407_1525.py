# Generated by Django 3.2.12 on 2023-04-07 07:25

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("analyze", "0002_alter_analyzeconfig_analyze_type"),
    ]

    operations = [
        migrations.AddField(
            model_name="analyzeconfig",
            name="priority_index",
            field=models.IntegerField(default=int, verbose_name="优先顺序"),
        ),
        migrations.AlterField(
            model_name="analyzeconfig",
            name="analyze_type",
            field=models.CharField(choices=[("normal", "常规策略"), ("aiops", "AI策略")], max_length=64, verbose_name="分析类型"),
        ),
    ]