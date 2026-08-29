from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        # NOTE: update this to your actual latest migration in the referrals app,
        # e.g. ('referrals', '0001_initial'),
        ('referrals', '0001_initial'),
    ]

    operations = [
        migrations.AddField(
            model_name='referralcode',
            name='wix_coupon_id',
            field=models.CharField(blank=True, default='', max_length=100),
        ),
        migrations.AddField(
            model_name='referralcode',
            name='wix_sync_status',
            field=models.CharField(
                choices=[
                    ('not_applicable', 'Not Applicable'),
                    ('pending', 'Not Yet Synced'),
                    ('synced', 'Synced to Wix'),
                    ('failed', 'Sync Failed'),
                ],
                default='not_applicable',
                max_length=15,
            ),
        ),
        migrations.AddField(
            model_name='referralcode',
            name='wix_sync_error',
            field=models.TextField(blank=True, default=''),
        ),
        migrations.AddField(
            model_name='referralcode',
            name='wix_last_synced_at',
            field=models.DateTimeField(blank=True, null=True),
        ),
        migrations.AlterField(
            model_name='auditlog',
            name='action',
            field=models.CharField(
                choices=[
                    ('requested', 'Code Requested'),
                    ('approved', 'Code Approved'),
                    ('rejected', 'Code Rejected'),
                    ('edited', 'Code Edited'),
                    ('deactivated', 'Code Deactivated'),
                    ('deleted', 'Code Deleted'),
                    ('redeemed', 'Code Redeemed At Checkout'),
                    ('user_created', 'User Account Created'),
                    ('wix_sync_failed', 'Wix Sync Failed'),
                ],
                max_length=20,
            ),
        ),
    ]
