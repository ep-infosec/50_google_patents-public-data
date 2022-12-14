# Copyright 2018 Google Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Generate a report on the database tables.
#
# $ python3 dataset_report.pysh --project_id=<my-project> --configs dataset_public.json dataset_uspto.json ... --output_dir=../tables --formats=pdf
import sh
import sys
import re
import os
import json
import collections
import datetime
import jinja2
import argparse

parser = argparse.ArgumentParser(description="Generate a set of documentation pages for BigQuery tables.")
parser.add_argument("--project_id", help="Project ID used to query tables.")
parser.add_argument("--configs", nargs="+", help="List of JSON configuration files.")
parser.add_argument("--output_dir", help="Output directory for files.")
args = parser.parse_args()

if not args.output_dir:
  print("--output_dir is required")
  sys.exit(1)
if not args.project_id:
  print("--project_id is required")
  sys.exit(1)

output_dir = os.path.expanduser(args.output_dir)

bq = sh.Command("bq")

# Read config files.
table_config = {}
group_config = {}
join_config = {}

for name in args.configs:
  print("Reading config %s" % name)
  with open(os.path.expanduser(name), "r") as f:
    try:
      c = json.loads(f.read())
    except Exception as e:
      print("Error parsing JSON (this is usually caused by a trailing comma)")
      raise e
    for k, v in c.get("tables", {}).items():
      if k in table_config:
        table_config[k].extend(v)
      else:
        table_config[k] = v
    group_config.update(c.get("groups", {}))
    for k, v in c.get("joins", {}).items():
      if k in join_config:
        join_config[k].extend(v)
      else:
        join_config[k] = v

print(table_config)
print(group_config)
print(join_config)

# Keep track of printed objects from __repr__.
__repr_recursion_set = None

def namedtuple(name, field_list):
  fields = field_list.split(" ")
  def init(self, **kwargs):
    for k, v in kwargs.items():
      if not k in fields:
        raise AttributeError("%s not in %s" % (k, fields))
      setattr(self, k, v)
  def repr(self):
    global __repr_recursion_set
    top = False
    if not __repr_recursion_set:
      top = True
      __repr_recursion_set = set()
    if self in __repr_recursion_set:
      result = "%s<...>" % name
    else:
      __repr_recursion_set.add(self)
      result = "%s<%s>" % (name, ", ".join(["%s=%s" % (k, getattr(self, k)) for k in fields]))
    if top:
      __repr_recursion_set = None
    return result
  return type(name, (), dict({k: None for k in fields}, __init__=init, __repr__=repr))

Dataset = namedtuple("Dataset", "name last_updated tables")

Table = namedtuple("Table", "name version dataset_description description dataset fields last_updated num_rows from_joins num_bytes old_version")

Field = namedtuple("Field", "name table description type mode from_joins to_joins")

Join = namedtuple("Join", "name from_field to_field percent num_rows join_stats sql")

JoinStat = namedtuple("JoinStat", "percent num_rows key sample_value")

datasets = collections.OrderedDict()

def find_field(table_name, column):
  for dataset in datasets.values():
    for t in dataset.tables:
      if t.name == table_name:
        for f in t.fields:
          if column == f.name:
            return f
  return None

def ts_to_string(unix):
  return datetime.datetime.utcfromtimestamp(unix).strftime("%Y-%m-%d")

def tsql(table):
  return table.replace(":", ".")

# Fetch a list of all tables and schemas for those tables.
for nice_name, table_fmts in table_config.items():
  for table_fmt in table_fmts:
    dataset_name, table_name = table_fmt.split(".")
    if nice_name not in datasets:
      dataset = Dataset(name=nice_name)
      datasets[nice_name] = dataset
    else:
      dataset = datasets[nice_name]
    show_info = json.loads(bq("--format=prettyjson", "--project_id", args.project_id, "show", dataset_name).stdout.decode('utf-8'))

    if not dataset.tables:
      dataset.tables = []

    print("Loading dataset %s" % dataset_name)
    tables = json.loads(bq("--format=prettyjson", "--project_id", args.project_id, "ls", "-n", "100000", dataset_name).stdout.decode('utf-8'))
    for table_data in tables:
      name = table_data["tableReference"]["tableId"]
      if re.match(table_name.replace("*", ".*"), name):
        table = Table(name=dataset_name + "." + name, dataset=dataset, dataset_description=show_info.get("description", ""))
        dataset.tables.append(table)
        print(table.name)

# Detect table and dataset versions, mark older versions.
latest_table_base = {}  # map[base name]latest name
no_version_tables = {}
for dataset in datasets.values():
  for table in dataset.tables:
    def sub_fn(m):
      return m.group(1)
    m = re.match("^(.+)_([0-9]+[0-9a-zA-Z]*)", table.name)
    if not m:
      no_version_tables[table.name] = True
      latest_table_base[table.name] = table.name
    else:
      base = m.group(1)
      table.version = m.group(2)
      if not base in latest_table_base:
        latest_table_base[base] = table.name
      elif latest_table_base[base] < table.name and not no_version_tables.get(base, ""):
        latest_table_base[base] = table.name


latest_tables = {}
for latest in latest_table_base.values():
  latest_tables[latest] = True

for dataset in datasets.values():
  for table in dataset.tables:
    if table.name not in latest_tables:
      table.old_version = True

for dataset in datasets.values():
  for table in dataset.tables:
    if table.old_version:
      print("Skipping old table %s" % table.name)
      continue
    print("Loading table %s" % table.name)
    table_info = json.loads(bq("--format=prettyjson", "--project_id", args.project_id, "show", table.name).stdout.decode('utf-8'))
    table_fields = []
    def add_fields(parent, fields):
      for field in fields:
        name = field["name"]
        if parent:
          name = parent + "." + name
        table_fields.append(Field(
            name=name,
            table=table,
            description=field.get("description", ""),
            type=field.get("type", ""),
            mode=field.get("mode", ""),
        ))
        if "fields" in field:
          add_fields(name, field["fields"])

    add_fields("", table_info["schema"]["fields"])
    table.fields = table_fields
    table.description = table_info.get("description", "")
    table.last_updated = ts_to_string(int(table_info["lastModifiedTime"]) / 1000)
    if not dataset.last_updated or dataset.last_updated < table.last_updated:
      dataset.last_updated = table.last_updated
    table.num_rows = table_info["numRows"]
    table.num_bytes = table_info["numBytes"]
    # Possibly calculate group-by stats.
    if table.name in group_config:
      column = group_config[table.name]
      query = "SELECT COUNT(*) AS cnt, {column} AS grouped FROM `{table}` GROUP BY 2 ORDER BY 1".format(table=tsql(table.name), column=column)
      result = json.loads(bq("--format=prettyjson", "--project_id", args.project_id, "query", "--use_legacy_sql=false", query).stdout.decode('utf-8'))
      table.stats = {}
      for row in result:
        js = JoinStat(key=row["grouped"], num_rows=int(row["cnt"]))
        table.stats[js.key] = js


# Support wildcards in join groups: dataset:*|molregno
for join_group in join_config.values():
  i = 0
  while i < len(join_group):
    if not "*" in join_group[i]:
      i += 1
      continue
    table_fmt, column_fmt = join_group[i].split("|")
    # Loop over all tables and columns and look for matches.
    matches = []
    for dataset in datasets.values():
      for table in dataset.tables:
        if not re.match(table_fmt.replace("*", ".*"), table.name) or table.old_version:
          continue
        for field in table.fields:
          if re.match(column_fmt.replace("*", ".*"), field.name):
            matches.append("%s|%s" % (table.name, field.name))
    # Replace join_group[i] with the matched values.
    join_group.pop(i)
    for v in matches:
      join_group.insert(i, v)
      i += 1

join_done = set()

# Enumerate all possible joins inside each group of matching columns.
for join_name, join_group in join_config.items():
  for i in range(len(join_group)):
    self = join_group[i]
    for j in range(len(join_group)):
      if j == i:
        continue
      first_table, first_column = join_group[i].split("|")
      second_table, second_column = join_group[j].split("|")
      # Only join tables if one or more has a + as the prefix.
      if not first_table.startswith("+") and not second_table.startswith("+"):
        continue
      first_table = first_table.lstrip("+")
      second_table = second_table.lstrip("+")
      key = first_table + first_column + second_table + second_column
      if key in join_done or (first_table == second_table and first_column == second_column):
        continue
      join_done.add(key)
      print("Running join between %s and %s" %  (join_group[i], join_group[j]))
      from_field = find_field(first_table, first_column)
      to_field = find_field(second_table, second_column)
      if not from_field or not to_field:
        raise TypeError("fields not found: %s:%s %s:%s" % (join_group[i], from_field is not None, join_group[j], to_field is not None))
      group_by = group_config.get(first_table, None)
      if not group_by:
        query = """#standardSQL
SELECT
  COUNT(*) AS cnt,
  COUNT(second.second_column) AS second_cnt,
  ARRAY_AGG(first.{first_column} IGNORE NULLS ORDER BY RAND() LIMIT 5) AS sample_value
FROM `{first_table}`AS first
LEFT JOIN (
  SELECT {second_column} AS second_column, COUNT(*) AS cnt
  FROM `{second_table}`
  GROUP BY 1
) AS second ON first.{first_column} = second.second_column""".format(first_table=tsql(first_table), first_column=first_column, second_table=tsql(second_table), second_column=second_column)
      else:
        query = """#standardSQL
SELECT
  COUNT(*) AS cnt,
  COUNT(second.second_column) AS second_cnt,
  first.{group_by} AS grouped,
  ARRAY_AGG(first.{first_column} IGNORE NULLS ORDER BY RAND() LIMIT 5) AS sample_value
FROM `{first_table}`AS first
LEFT JOIN (
  SELECT {second_column} AS second_column, COUNT(*) AS cnt
  FROM `{second_table}`
  GROUP BY 1
) AS second ON first.{first_column} = second.second_column
GROUP BY 3""".format(first_table=tsql(first_table), first_column=first_column, second_table=tsql(second_table), second_column=second_column, group_by=group_by)

      result = json.loads(bq("--format=prettyjson", "query", "--use_legacy_sql=false", query).stdout.decode('utf-8'))
      total_rows = 0
      joined_rows = 0

      join_stats = {}
      join = Join(name=join_name, from_field=from_field, to_field=to_field, join_stats=join_stats, sql=query)
      if not from_field.from_joins:
        from_field.from_joins = []
      from_field.from_joins.append(join)
      if not to_field.to_joins:
        to_field.to_joins = []
      to_field.to_joins.append(join)
      if not from_field.table.from_joins:
        from_field.table.from_joins = []
      from_field.table.from_joins.append(join)
      for row in result:
        cnt = int(row["cnt"])
        second_cnt = int(row["second_cnt"])
        total_rows += cnt
        joined_rows += second_cnt
        if not group_by:
          join_stats[""] = JoinStat(percent=second_cnt / cnt, num_rows=second_cnt, key="all", sample_value=row["sample_value"])
        else:
          join_stats[row["grouped"]] = JoinStat(percent=second_cnt / cnt, num_rows=second_cnt, key=row["grouped"], sample_value=row["sample_value"])
      join.percent = joined_rows / total_rows
      join.num_rows = joined_rows

def other_formats(name):
  if not args.formats:
    return
  for fmt in args.formats.split(","):
    sh.pandoc(name, "--from", "markdown", "-s", "-o", "%s.%s" % (name, fmt))

# "index.md"
# Links to every dataset and description of each dataset
# DOT graph of links between tables
# Link statistics: % of rows that link together
main_page_template = jinja2.Template("""
---
geometry: margin=0.6in
---

# Datasets

{% for dataset in datasets.values() %}
## [{{dataset.name}}](dataset_{{dataset.name}}.md)

| Name | Last updated | Rows | Joins |
|-------------------------------------------|-------|--------|-----------------|
{% for table in dataset.tables -%}
| [{{table.name}}](https://bigquery.cloud.google.com/table/{{table.name}}) | {% if table.last_updated %}{{table.last_updated }}{% endif %} | {% if table.num_rows %}{{"{0:,}".format(table.num_rows|int)}}{% endif %} |
{%- if table.from_joins %}{% for group in table.from_joins|groupby("name") -%}
{{group.grouper}} {% endfor %}{% endif %} |
{% endfor %}
{% endfor %}
""")

index_output = os.path.join(output_dir, "index.md")
with open(index_output, "w") as f:
  f.write(main_page_template.render(datasets=datasets))
other_formats(index_output)

# "dataset_<name>.md"
# Description of dataset
# List of all tables in dataset
# Sample rows in each table
# Links to other datasets
# Inner-dataset links
# DOT graph of links
dataset_page_template = jinja2.Template("""
---
geometry: margin=0.6in
---

# {{dataset.name}}

{% for table in dataset.tables %}
*****
## {{table.name}}

{% if table.old_version %}
Old table version `{{ table.version }}`, schema skipped.
{% else %}
{% if table.dataset_description %}
> {{table.dataset_description|replace("\n", "\n> ")}}
{% endif %}
{% if table.description %}
> {{table.description|replace("\n", "\n> ")}}
{% endif %}
{% endif %}

{% if table.fields %}
| Stat | Value |
|----------|----------|
| Last updated | {{table.last_updated}} |
| Rows | {{"{0:,}".format(table.num_rows|int)}} |
| Size | {{table.num_bytes|filesizeformat}} |

### Schema
[View in BigQuery](https://bigquery.cloud.google.com/table/{{table.name}})

{% for field in table.fields -%}
* `{{field.name}}` {{field.type}} {{field.mode}} {% if field.from_joins %} joins on **{{ field.from_joins[0].name }}**{% endif %}
{% if field.description %}
    > {{field.description|replace("\n", "\n> ")}}
{% endif %}
{% endfor %}

{% if table.from_joins %}### Join columns{% endif %}
{% for field in table.fields %}
{% if field.from_joins %}
#### {{field.name}}
{% for join in field.from_joins %}
joins to `{{ join.to_field.table.name }}::{{ join.to_field.name }}` on **{{ join.name }}** ({{"%.2f" % (100 * join.percent)}}%, {{"{0:,}".format(join.num_rows|int)}} rows)

| Key | Percent | Rows | Sample values |
|------|-----|--------|--------------------------------------------------------|
{% for stat in join.join_stats.values() -%}
| `{{stat.key}}` | {% if stat.percent > 0.0 %}{{"%.2f" % (100 * stat.percent)}}%{% else %}*none*{% endif %} | {{"{0:,}".format(stat.num_rows|int)}} | `{{stat.sample_value}}` |
{% endfor %}

    {{join.sql|indent}}

{% endfor %}
{% for join in field.to_joins %}
joins from `{{ join.from_field.table.name }}::{{ join.from_field.name }}` on **{{ join.name }}** ({{"%.2f" % (100 * join.percent)}}%, {{"{0:,}".format(join.num_rows|int)}} rows)
{% endfor %}
{% endif %}
{% endfor %}
{% endif %}

{% endfor %}
""")

for dataset in datasets.values():
  output = os.path.join(output_dir, "dataset_%s.md" % dataset.name)
  with open(output, "w") as f:
    f.write(dataset_page_template.render(dataset=dataset))
  other_formats(output)
