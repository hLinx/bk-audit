# Generated by Django 3.2.18 on 2023-07-04 02:37

from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("risk", "0001_initial"),
    ]

    operations = [
        migrations.AlterField(
            model_name="risk",
            name="operator",
            field=models.JSONField(blank=True, null=True, verbose_name="负责人"),
        ),
    ]