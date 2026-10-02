"""Les lectures qui VÉRIFIENT la couche `origine` la demandent : le défaut ne sert que
la valeur actuelle (décision du 02/10/2026). Un seul endroit pour dire « avec l'origine »."""
from oto_mcp.datastore.core import DatastorePg

AVEC_ORIGINE = ("current", "origine")


def row_to_dict_avec_origine(*args, **kwargs):
    kwargs.setdefault("versions", AVEC_ORIGINE)
    return DatastorePg._row_to_dict(*args, **kwargs)
