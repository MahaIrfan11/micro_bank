from django.urls import path

from .views import (
    AccountDepositView,
    AccountDetailView,
    AccountListCreateView,
    AccountTransactionsView,
    TransferDetailView,
    TransferView,
)

urlpatterns = [
    path("", AccountListCreateView.as_view()),
    path("transfers/", TransferView.as_view()),
    path("transfers/<uuid:id>/", TransferDetailView.as_view()),
    path("<str:account_number>/", AccountDetailView.as_view()),
    path("<str:account_number>/transactions/", AccountTransactionsView.as_view()),
    path("<str:account_number>/deposit/", AccountDepositView.as_view()),
]
