from pathlib import Path
from .core import ROOT,VAULT
from .writer_core import Writer
from .workspace import active_workspace
ACCEPTANCE_ROOT=active_workspace().cache/"production-writer-acceptance"
ACCEPTANCE_VERSIONS=ACCEPTANCE_ROOT/"versions"
REAL_VERSIONS=active_workspace().versions/"formal-memory"
def writer(mode="acceptance"):
 if mode=="acceptance":return Writer(ACCEPTANCE_ROOT/"vault",ACCEPTANCE_VERSIONS)
 if mode=="production":return Writer(VAULT,REAL_VERSIONS)
 raise ValueError("Writer模式无效")
def preview(relative,title,content,evidence=None,supersedes=None,mode="acceptance",**options):return writer(mode).preview(relative,title,content,evidence,supersedes,**options)
def apply(item,owner_confirmed=False,mode="acceptance",validate=None):return writer(mode).apply(item,owner_confirmed,validate)
def undo(version,owner_confirmed=False,mode="acceptance"):return writer(mode).undo(version,owner_confirmed)
