# 관리자 다국어 표시명 반영(개발 main 병합 후 번호 재정렬)

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0018_board_named_indexes'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='board',
            options={'verbose_name': '보드', 'verbose_name_plural': '보드'},
        ),
        migrations.AlterModelOptions(
            name='pin',
            options={'verbose_name': 'Pin', 'verbose_name_plural': 'Pin'},
        ),
    ]
