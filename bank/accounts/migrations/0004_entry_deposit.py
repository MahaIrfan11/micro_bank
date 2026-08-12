# Hand-written (device_bash can't reach the venv from this sandbox -- its
# symlinks point at Mac-native absolute paths). Mirrors exactly what
# `manage.py makemigrations accounts` would generate for the Entry changes
# in accounts/models.py: transfer becomes nullable, deposit is added, and a
# constraint enforces exactly one of the two is set per entry.

import django.db.models.deletion
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('accounts', '0003_account_account_type_and_more'),
    ]

    operations = [
        migrations.AlterField(
            model_name='entry',
            name='transfer',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='entries', to='accounts.transfer'),
        ),
        migrations.AddField(
            model_name='entry',
            name='deposit',
            field=models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.PROTECT, related_name='entries', to='accounts.deposit'),
        ),
        migrations.AddConstraint(
            model_name='entry',
            constraint=models.CheckConstraint(
                condition=(
                    models.Q(('transfer__isnull', False), ('deposit__isnull', True))
                    | models.Q(('transfer__isnull', True), ('deposit__isnull', False))
                ),
                name='entry_has_exactly_one_origin',
            ),
        ),
    ]
