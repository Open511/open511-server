from django.conf import settings

from appconf import AppConf


class Open511Settings(AppConf):

    ENABLE_TEST_ENDPOINT = False