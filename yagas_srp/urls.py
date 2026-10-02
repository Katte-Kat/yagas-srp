from django.urls import path

from . import views

app_name = "yagas_srp"

urlpatterns = [
    path("", views.request_list, name="request_list"),
    path("request/<int:pk>/", views.request_detail, name="request_detail"),
    path("request/<int:pk>/review", views.review_request, name="review_request"),
    path("request/<int:pk>/reprice", views.reprice, name="reprice"),
    path("poll", views.poll_now, name="poll_now"),
    path("receivers/", views.receivers, name="receivers"),
    path("receivers/select", views.select_receiver, name="select_receiver"),
    path("receivers/<int:pk>/update", views.update_receiver, name="update_receiver"),
    path("receiver/add", views.add_receiver, name="add_receiver"),
]
