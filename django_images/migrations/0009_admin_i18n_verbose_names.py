# 관리자 다국어 표시명 반영(개발 main 병합 후 번호 재정렬)

from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('django_images', '0008_image_animation_status'),
    ]

    operations = [
        migrations.AlterModelOptions(
            name='image',
            options={'verbose_name': '이미지', 'verbose_name_plural': '이미지'},
        ),
        migrations.AlterModelOptions(
            name='thumbnail',
            options={'verbose_name': '썸네일', 'verbose_name_plural': '썸네일'},
        ),
    ]
