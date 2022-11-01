import hashlib
import importlib
import json
import lzma
import pickle
import tempfile
from concurrent.futures import Future, ThreadPoolExecutor, wait
from copy import deepcopy
from pathlib import Path
from string import Template
from typing import Union

import esparto
import pandas
import seaborn
import tinycss2
from cloudpathlib import AnyPath
from IPython import display
from pathvalidate import sanitize_filepath

from .api import OutputFormat, analytics_query, config, list_workspaces, cache, api_2, logger


def prep_report(name, queries, expires):
    """Prepare a report for a workspace"""
    kp = KQL()
    kp.set_agency(name)
    kp.load_queries(queries, caching=expires)


@api_2.post("/load_report_queries")
def batch_reporting(queries: dict, expires="1 day"):
    """Run reporting on all workspaces"""
    for name in list_workspaces(format=OutputFormat.df)["SecOps Group"].dropna().unique():
        if name:
            prep_report(name, queries, expires)
    prep_report("ALL", queries, expires)


class KQL:
    base_css = tinycss2.parse_stylesheet(open(esparto.options.esparto_css).read())
    pdf_css = Template(importlib.resources.read_text(f"{__package__}.templates", "esparto-pdf.css"))

    sns = seaborn

    def __init__(self, path: Union[Path, AnyPath] = None, template: str = "", subfolder: str = "notebooks", timespan: str = "P30D"):
        """
        Convenience tooling for loading pandas dataframes using context from a path.
        path is expected to be pathlib type object with a structure like below:
        .
        `--{subfolder} (default is notebooks)
           |--kql
           |  |--*/*.kql
           |--lists
           |  |--SentinelWorkspaces.csv
           |  `--SecOps Groups.csv
           |--markdown
           |  `--**.md
           `--reports
              `--*/*/*.pdf
        """
        if not path:
            path = config("datalake_path")
        self.pdf_css_file = tempfile.NamedTemporaryFile(delete=False, mode="w+t", suffix=".css")
        self.timespan, self.path, self.nbpath = timespan, path, path / sanitize_filepath(subfolder)
        self.kql, self.lists, self.reports = self.nbpath / "kql", self.nbpath / "lists", self.nbpath / "reports"
        self.sentinelworkspaces = list_workspaces()
        self.wsdf = list_workspaces(format="df")
        self.today = pandas.Timestamp("today")
        if template:
            self.load_templates(mdpath=template)

    def set_agency(self, agency: str, sample_agency: str = "", sample_only: bool = False):
        # if sample_only = True, build report with only mock data
        # if sample_only = False, build report as usual, only substituting missing data with sample data
        # sections should 'anonymise' sample data prior to rendering
        self.agency = agency
        if agency == "ALL":
            self.agency_name = "Overview"
        else:
            self.agency_info = self.wsdf[self.wsdf["SecOps Group"] == agency]
            self.agency_name = self.agency_info["Primary agency"].max()
            self.sentinelworkspaces = list(self.agency_info.customerId.dropna())
        self.sample_agency = sample_agency
        self.sample_only = sample_only
        if self.sample_agency:
            self.sampleworkspaces = list(self.wsdf[self.wsdf["SecOps Group"] == sample_agency].customerId.dropna())
        else:
            self.sampleworkspaces = False
        return self

    def load_queries(self, queries: dict({str: str}), caching: str = None):
        """
        load a bunch of kql into dataframes
        """
        queries = deepcopy(queries)
        kusto_key = hashlib.sha256(json.dumps(queries, sort_keys=True).encode()).hexdigest()
        cache_key = f"{self.agency}_{kusto_key}.lzma"  # cache is valid for 1 day
        query_cache = self.nbpath / "query_cache" / cache_key
        if caching and query_cache.exists() and query_cache.stat().st_mtime > (self.today - pandas.Timedelta(caching)).timestamp():
            logger.info(f"Loading cached queries from {query_cache}")
            cacheitem = pickle.loads(lzma.decompress(query_cache.read_bytes()))
            self.queries = cacheitem["queries"]
            self.querystats = cacheitem["querystats"]
            return
        kusto = cache.get(kusto_key)
        if kusto is None:
            kusto = {k: (self.kql / sanitize_filepath(v)).read_text() for k, v in queries.items()}
            cache.set(kusto_key, kusto, ttl=60 * 60 * 24)
        querystats = {}
        with ThreadPoolExecutor(max_workers=config("max_threads")) as executor:
            logger.info(
                f"Running {len(queries.keys())} queries across {self.agency_name}: {len(self.sentinelworkspaces)} workspaces (sample: {self.sample_agency}): "
            )
            for key, kql in queries.items():
                if self.sample_only:
                    # force return no results to fallback to sample data
                    query = kusto[key]
                    table = query.split("\n")[0].split(" ")[0].strip()
                    df = pandas.DataFrame([{f"{table}": f"No Data in timespan {self.timespan}"}])
                    queries[key] = (kql, df)
                    querystats[key] = [0, f"{df.columns[0]} - {df.iloc[0,0]}", kql]
                else:
                    queries[key] = (kql, executor.submit(self.kql2df, kusto[key]))
            wait([f for kql, f in queries.values() if isinstance(f, Future)])
            queries.update({key: (kql, future.result()) for key, (kql, future) in queries.items() if isinstance(future, Future)})
            for key, (kql, df) in queries.items():
                if df.shape == (1, 1) and df.iloc[0, 0].startswith("No Data"):
                    querystats[key] = [0, f"{df.columns[0]} - {df.iloc[0,0]}", kql]
                    if self.sampleworkspaces:
                        queries[key] = (kql, executor.submit(self.kql2df, kusto[key], workspaces=self.sampleworkspaces))
                else:
                    querystats[key] = [df.count().max(), len(df.columns), kql]
            wait([f for kql, f in queries.values() if isinstance(f, Future)])
            queries.update({key: (kql, future.result()) for key, (kql, future) in queries.items() if isinstance(future, Future)})
            self.queries = queries
        self.querystats = pandas.DataFrame(querystats).T.rename(columns={0: "Rows", 1: "Columns", 2: "KQL"}).sort_values("Rows")
        if caching:
            cacheitem = {"querystats": self.querystats, "queries": self.queries}
            data = lzma.compress(pickle.dumps(cacheitem))
            query_cache.write_bytes(data)

    def load_templates(self, mdpath: str):
        """
        Reads a markdown file, and converts into a dictionary
        of template fragments and a report title.

        Report title set based on h1 title at top of document
        Sections split with a horizontal rule, and keys are set based on h2's.
        """
        md_tmpls = (self.nbpath / mdpath).open().read().split("\n---\n\n")
        md_tmpls = [tmpl.split("\n", 1) for tmpl in md_tmpls]
        self.report_title = md_tmpls[0][0].replace("# ", "")
        self.report_sections = {title.replace("## ", ""): Template(content) for title, content in md_tmpls[1:]}

    def init_report(self, font=["Arial"], table_of_contents=True, **kwargs):
        if len(self.sentinelworkspaces) == 0:
            raise Exception("No workspaces to query, report generation failed.")
        # Return an esparto page for reporting after customising css and style seaborn / matplotlib
        self.sns.set_theme(
            style="darkgrid",
            context="paper",
            font=font,
            font_scale=0.7,
            rc={"figure.figsize": (7, 3), "figure.constrained_layout.use": True, "legend.loc": "upper right"},
        )
        pandas.set_option("display.max_colwidth", None)
        kwargs["font"] = ", ".join([f'"{f}"' for f in font])
        self.css_params = kwargs

        base_css = [r for r in self.base_css if not hasattr(r, "at_keyword")]  # strip media/print styles so we can replace

        bg = self.css_params["background"]
        self.background_file = tempfile.NamedTemporaryFile(delete=False, mode="w+b", suffix=bg.suffix)
        self.background_file.write(bg.open("r+b").read())
        self.background_file.flush()
        self.css_params["background"] = f"file://{self.background_file.name}"
        extra_css = tinycss2.parse_stylesheet(self.pdf_css.substitute(title=self.report_title, **self.css_params))
        for rule in base_css + extra_css:
            self.pdf_css_file.write(rule.serialize())
        self.pdf_css_file.flush()
        self.report = esparto.Page(
            title=self.report_title, table_of_contents=table_of_contents, output_options=esparto.OutputOptions(esparto_css=self.pdf_css_file.name)
        )
        # css not appearing to be utilised correctly, fallback global set
        esparto.options.esparto_css = self.pdf_css_file.name
        return self.report

    def report_pdf(self, preview=True, folders=True, savehtml=False):
        if folders:
            report_dir = self.reports / self.agency
        else:
            report_dir = self.reports
        report_dir.mkdir(parents=True, exist_ok=True)

        self.pdf_file = report_dir / f"{self.report_title.replace(' ','')}-{self.agency}-{self.today.strftime('%b%Y')}.pdf"
        with tempfile.NamedTemporaryFile(mode="w+b", suffix=".pdf") as pdf_file_tmp:
            self.html = self.report.save_pdf(pdf_file_tmp, return_html=True)
            pdf_file_tmp.seek(0)
            self.pdf_file.write_bytes(pdf_file_tmp.read())

        if savehtml:
            self.pdf_file.with_suffix(".html").write_text(self.html)

        self.excel_file = report_dir / f"{self.report_title.replace(' ','')}-{self.agency}-{self.today.strftime('%b%Y')}.xlsx"
        dfs = {}
        dfs["Query Stats"] = self.querystats
        for name, data in self.queries.items():
            # Cap exported rows at 2K to keep excel filesize sensible.
            if self.querystats["Rows"][name] == 0 or self.querystats["Rows"][name] > 2000:
                dfs[name] = pandas.DataFrame([self.querystats.loc[name]])
            else:
                dfs[name] = data[1]
        with tempfile.NamedTemporaryFile(mode="w+b", suffix=".xlsx") as excel_file_tmp:
            with pandas.ExcelWriter(excel_file_tmp) as writer:  # pylint: disable=abstract-class-instantiated
                for name, df in dfs.items():
                    date_columns = df.select_dtypes(include=["datetimetz"]).columns
                    for date_column in date_columns:
                        df[date_column] = df[date_column].dt.tz_localize(None)
                    df = df.drop("TableName", axis=1, errors="ignore")
                    df.to_excel(writer, sheet_name=name)
                    sheet = writer.sheets[name]
                    header_list = df.columns.values.tolist()  # Generate list of headers
                    for i in range(0, len(header_list)):
                        sheet.set_column(i, i, int(len(header_list[i]) * 1.5))
            excel_file_tmp.seek(0)
            self.excel_file.write_bytes(excel_file_tmp.read())
        if preview:
            return display.IFrame(self.pdf_file, width=1200, height=800)
        else:
            return self.pdf_file

    def kql2df(self, kql: str, timespan: str = "", workspaces: list[str] = []):
        # Load or directly query kql against workspaces
        # Parse results as json and return as a dataframe
        if not workspaces:
            workspaces = self.sentinelworkspaces
        table = kql.split("\n")[0].split(" ")[0].strip()
        try:
            data = analytics_query(workspaces=workspaces, query=kql, timespan=timespan or self.timespan)
            assert (len(data)) > 0
        except Exception as e:
            logger.warning(e)
            data = [{f"{table}": f"No Data in timespan {timespan}"}]
        df = pandas.DataFrame.from_dict(data)
        df = df[df.columns].apply(pandas.to_numeric, errors="ignore")
        if "TimeGenerated" in df.columns:
            df["TimeGenerated"] = pandas.to_datetime(df["TimeGenerated"])
        df = df.convert_dtypes()
        return df

    def rename_and_sort(self, df, names, rows=40, cols=40):
        # Rename columns based on dict
        df = df.rename(columns=names)
        # Merge common columns
        df = df.groupby(by=df.columns, axis=1).sum()
        # Sort columns by values, top 40
        df = df[df.sum(0).sort_values(ascending=False)[:cols].index]
        # Sort rows by values, top 40
        df = df.loc[df.sum(axis=1).sort_values(ascending=False)[:rows].index]
        return df

    @classmethod
    def label_size(cls, dataframe: pandas.DataFrame, category: str, metric: str, max_categories=9, quantile=0.5, max_scale=10, agg="sum", field="oversized"):
        """
        Annotates a dataframe based on quantile and category sizes, then groups small categories into other
        """
        df = dataframe.copy(deep=True)
        sizes = df.groupby(category)[metric].agg(agg).sort_values(ascending=False)
        maxmetric = sizes.quantile(quantile) * max_scale
        normal, oversized = sizes[sizes <= maxmetric], sizes[sizes > maxmetric]
        df[field] = df[category].isin(oversized.index)
        for others in (normal[max_categories:], oversized[max_categories:]):
            df[category] = df[category].replace({label: f"{others.count()} Others" for label in others.index})
        return df

    @classmethod
    def latest_data(cls, df: pandas.DataFrame, timespan: str, col="TimeGenerated"):
        """
        Return dataframe filtered by timespan
        """
        df = df.copy(deep=True)
        return df[df[col] >= (df[col].max() - pandas.to_timedelta(timespan))].reset_index()

    @classmethod
    def hash256(cls, obj, truncate: int = 16):
        return hashlib.sha256(pickle.dumps(obj)).hexdigest()[:truncate]

    @classmethod
    def hash_columns(cls, dataframe: pandas.DataFrame, columns: list):
        if not isinstance(columns, list):
            columns = [columns]
        for column in columns:
            dataframe[column] = dataframe[column].apply(KQL.hash256)

    def show(self, section: str):
        return display.HTML(self.report[section].to_html(notebook_mode=True))
