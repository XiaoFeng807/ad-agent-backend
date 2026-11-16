"""Server operator command: bind an owned local plan to a public artifact campaign."""

import argparse
from contextlib import closing
import json
from pathlib import Path
import sqlite3


def bind(business_db, artifact, plan_id, owner_id, source_plan_id):
    with closing(sqlite3.connect(Path(artifact).resolve().as_uri()+"?mode=ro",uri=True)) as source:
        manifest=json.loads(source.execute("SELECT manifest FROM metadata").fetchone()[0])
        if not source.execute("SELECT 1 FROM plan_daily_metrics WHERE source_plan_id=?",(source_plan_id,)).fetchone():
            raise ValueError("source campaign not found")
    with closing(sqlite3.connect(Path(business_db).resolve().as_uri()+"?mode=rw",uri=True)) as conn,conn:
        conn.execute("BEGIN IMMEDIATE")
        if not conn.execute("SELECT 1 FROM ad_plans WHERE id=? AND user_id=?",(plan_id,owner_id)).fetchone():
            raise ValueError("owned local plan not found")
        conn.execute("""INSERT INTO plan_metric_bindings VALUES (?,?,?,?) ON CONFLICT(plan_id) DO UPDATE SET
            source_id=excluded.source_id,data_version=excluded.data_version,source_plan_id=excluded.source_plan_id""",
            (plan_id,manifest["source_id"],manifest["data_version"],source_plan_id))
    return {"plan_id":plan_id,"source_id":manifest["source_id"],"data_version":manifest["data_version"],"source_plan_id":source_plan_id}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("business_db",type=Path)
    parser.add_argument("artifact",type=Path)
    parser.add_argument("--plan-id",type=int,required=True)
    parser.add_argument("--owner-id",type=int,required=True)
    parser.add_argument("--source-plan-id",required=True)
    args=parser.parse_args()
    print(json.dumps(bind(args.business_db,args.artifact,args.plan_id,args.owner_id,args.source_plan_id),ensure_ascii=False))
