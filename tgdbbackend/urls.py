"""tgdbbackend URL Configuration

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/1.11/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  url(r'^$', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  url(r'^$', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.conf.urls import url, include
    2. Add a URL to urlpatterns:  url(r'^blog/', include('blog.urls'))
"""
from django.conf import settings
from django.conf.urls import include, url
from django.conf.urls.static import static
from django.contrib import admin
from django.views import defaults as default_views

urlpatterns = [
                  # url(r'^$',
                  #     TemplateView.as_view(template_name='pages/home.html'),
                  #     name='home'),
                  # url(r'^about/$',
                  #     TemplateView.as_view(template_name='pages/about.html'),
                  #     name='about'),

                  # Django Admin, use {% url 'admin:index' %}
                  url(r'^admin/', admin.site.urls),

                  # User management
                  # url(r'^users/',
                  #     include('tgdbbackend.users.urls', namespace='users')),
                  # url(r'^accounts/', include('allauth.urls')),

                  # restful API
                  url(r"^api/", include(('tgdbbackend.targetDB.urls', 'targetDB'),
                                        namespace='targetDB')),
                  url(r'^api-auth/', include('rest_framework.urls',
                                             namespace='rest_framework')),

                  # Your stuff: custom urls includes go here
                  url(r'^queryapp/', include(('tgdbbackend.queryapp.urls', 'queryapp'),
                                             namespace='queryapp')),
                  url(r'^upload/', include('upload.urls'))
              ] + static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)

if settings.DEBUG:
    # This allows the error pages to be debugged during development, just visit
    # these url in browser to see how these error pages look like.
    import debug_toolbar

    urlpatterns += [
        url(r'^400/$', default_views.bad_request,
            kwargs={'exception': Exception('Bad Request!')}),
        url(r'^403/$', default_views.permission_denied,
            kwargs={'exception': Exception('Permission Denied')}),
        url(r'^404/$', default_views.page_not_found,
            kwargs={'exception': Exception('Page not Found')}),
        url(r'^500/$', default_views.server_error),
        url(r'__debug__', include(debug_toolbar.urls))
    ]
