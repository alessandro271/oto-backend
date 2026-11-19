"""Les jetons de lien — le lien public d'une page (`docs`) et le lien d'une procédure
partagée (`process_shares`) — rangés CHIFFRÉS, comme un secret du coffre.

Deux colonnes par lien :

- `*_enc` : le jeton chiffré par la clé maître (`crypto.encrypt`, la même enveloppe que
  `connector_credentials.secret_enc`, `key_ref` en tête). L'AAD est l'identité de la
  ligne (`aad_page`, `aad_partage`) : une enveloppe recopiée sur une autre ligne ne se
  déchiffre pas. Il ne sert qu'à RÉAFFICHER le lien à qui le gouverne ;
- `*_hash` : son empreinte (`tokens._hash_token`, SHA-256, celle des invitations),
  indexée et unique. Un lien ENTRANT se résout par elle, sans rien déchiffrer.

Le jeton n'est jamais écrit en clair : la colonne en clair d'avant (`docs.public_token`,
`process_shares.token`) n'est plus ni lue ni écrite que pour être vidée, et la commande
`oto-mcp maintenance jetons-de-lien` scelle ce qu'elle porte encore
(`jetons_de_lien_rangement`). Son retrait est une révision d'une PR suivante.

⚠️ Sans `OTO_MCP_MASTER_KEY`, sceller ou rouvrir LÈVE (`crypto`) : pas de lien en clair
de repli. Une enveloppe qui ne se déchiffre pas lève `EnveloppeIndechiffrable`.
"""
from __future__ import annotations

from typing import Optional

from .. import crypto
from .tokens import _hash_token


def aad_page(doc_id: int) -> str:
    return f"docs:{int(doc_id)}:public_token"


def aad_partage(share_id: int) -> str:
    return f"process_shares:{int(share_id)}:token"


def empreinte(jeton: str) -> str:
    """La clé de recherche d'un lien entrant (celle des invitations)."""
    return _hash_token(jeton)


def sceller(jeton: str, aad: str) -> str:
    return crypto.encrypt(jeton, aad)


def ouvrir(enveloppe: Optional[str], aad: str) -> Optional[str]:
    """Le jeton d'une enveloppe, None si la ligne n'en porte pas."""
    if enveloppe is None:
        return None
    return crypto.decrypt(enveloppe, aad)


#: Le DDL additif du démarrage (`db/_init.py`), dans cet ordre. Les colonnes en clair
#: restent posées jusqu'à leur révision de retrait ; celle des partages perd son
#: `NOT NULL` (le code ne l'écrit plus), sous garde de catalogue.
DDL_DEMARRAGE = (
    "ALTER TABLE docs ADD COLUMN IF NOT EXISTS public_token_enc TEXT",
    "ALTER TABLE docs ADD COLUMN IF NOT EXISTS public_token_hash TEXT",
    "CREATE UNIQUE INDEX IF NOT EXISTS idx_docs_public_token_hash ON docs(public_token_hash) "
    "WHERE public_token_hash IS NOT NULL",
    "ALTER TABLE process_shares ADD COLUMN IF NOT EXISTS token_enc TEXT",
    "ALTER TABLE process_shares ADD COLUMN IF NOT EXISTS token_hash TEXT",
    """DO $$
BEGIN
  IF EXISTS (SELECT 1 FROM information_schema.columns
             WHERE table_schema = current_schema() AND table_name = 'process_shares'
               AND column_name = 'token' AND is_nullable = 'NO') THEN
    ALTER TABLE process_shares ALTER COLUMN token DROP NOT NULL;
  END IF;
END $$""",
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_process_shares_token_hash "
    "ON process_shares(token_hash) WHERE token_hash IS NOT NULL",
)
