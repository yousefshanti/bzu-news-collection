# Data

`articles.db` (SQLite, 52,670 articles, 13 sources) is stored gzipped and split into 45 MB parts.

Rebuild:

    cat data/articles.db.gz.part* | gunzip > articles.db
