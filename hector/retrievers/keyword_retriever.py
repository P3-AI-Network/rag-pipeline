import logging
import json 

from typing import List, Dict
from psycopg2.extensions import cursor
import psycopg2.extras
from uuid import uuid4

from langchain_core.documents import Document
from langchain_core.embeddings import Embeddings

from utils.base import fetch_documents, rank_keywords
from core.base import BaseRetriever

class KeywordRetriever(BaseRetriever):

    def __init__(self, cursor: cursor, embeddings: Embeddings):
        self.cursor = cursor
        self.embeddings = embeddings

    def get_relevant_documents(self, query: str, document_limit: int) -> List[Document]:
                
        doc_ranking = self.kw_search_with_ranking(query, document_limit)
        docs = doc_ranking.keys()
        return fetch_documents(list(docs))[:document_limit]
        

    def kw_search_with_ranking(self, query: str, document_limit: int) -> Dict[str, int]:

        score_uid_info = self.kw_search_with_score(query, document_limit)

        scores = [] # [<score_doc_1>, <score_doc_2>]

        for score in score_uid_info.values():
            scores.append(score)

        ranking = rank_keywords(scores)

        for index, key in enumerate(score_uid_info):
            score_uid_info[key] = ranking[index]
        
        return score_uid_info

    def kw_search_with_score(self, query: str, document_limit: int) -> Dict[str, float]:

        sql = """
            SELECT uuid, document, ts_rank_cd(search_vector, query) AS rank
            FROM langchain_pg_embedding, plainto_tsquery(%s) query
            WHERE search_vector @@ query
            ORDER BY rank DESC
            LIMIT %s;
        """

        self.cursor.execute(sql, (query,document_limit))

        results = self.cursor.fetchall()

        score_uid_info = {} # {'<uuid>': <score>}

        for row in results:
            score_uid_info[row[0]] = row[-1]
        
        return score_uid_info
    
    def add_documents(self, documents: List[Document]):

        sql = """
            INSERT INTO langchain_pg_embedding (
                collection_id, embedding, document, cmetadata, custom_id
            ) VALUES %s;
        """

        text_docs = [doc.page_content for doc in documents]
        embeddings = self.embeddings.embed_documents(text_docs)

        insert_data = []

        for doc, embedding in zip(documents, embeddings):
            insert_data.append((self.collection_uuid, embedding, doc.page_content, json.dumps(doc.metadata), str(uuid4())))

        psycopg2.extras.execute_values(self.cursor, sql, insert_data, template="(%s, %s, %s, %s, %s)")
        logging.info("Documents Inserted!")


    def init_tables(self):
         
        """
            If tables don't exist, initializes `langchain_pg_collection` 
            and `langchain_pg_embedding` with appropriate datatypes.
        """


        create_tables_sql = f"""
            BEGIN;
            CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

            -- Create langchain_pg_collection if it does not exist
            CREATE TABLE IF NOT EXISTS langchain_pg_collection (
                uuid UUID PRIMARY KEY DEFAULT uuid_generate_v4(),
                name VARCHAR(255) NOT NULL,
                cmetadata JSON
            );

            -- Create langchain_pg_embedding if it does not exist
            CREATE TABLE IF NOT EXISTS langchain_pg_embedding (
                collection_id UUID NOT NULL,
                embedding VECTOR({self.embedding_dimension}),
                document VARCHAR(1000),
                cmetadata JSON,
                custom_id VARCHAR,
                uuid UUID PRIMARY KEY DEFAULT uuid_generate_v4()
            );

            -- Add search_vector column if it does not exist
            DO $$ 
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM information_schema.columns 
                    WHERE table_name = 'langchain_pg_embedding' AND column_name = 'search_vector'
                ) THEN
                    ALTER TABLE langchain_pg_embedding 
                    ADD COLUMN search_vector tsvector GENERATED ALWAYS AS (to_tsvector('english', document)) STORED;
                END IF;
            END $$;

            COMMIT;
        """
        try:
            self.cursor.execute(create_tables_sql)
            logging.info("Initial tables created")
        except:
            logging.info("Initial tables already exists")