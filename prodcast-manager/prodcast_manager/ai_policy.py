"""AI package selection must never implicitly disable an existing deployment."""
import json


def preserve_existing_ai(config,vault,directory,log=lambda text:None):
    if config.get('install_ai',True):return False
    path=directory/'ai-journal.json'
    journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
    installed=journal.get('status')=='complete' or vault.data.get('ai_installed',False)
    if not installed:return False
    config['install_ai']=True
    log('AI is already installed: preserving App integration and DB/network access. Skipping AI packages does not disable AI. Existing AI runtime and models are not reinstalled.')
    return True


def can_reconnect_existing_ai(vault,directory):
    path=directory/'ai-journal.json'
    journal=json.loads(path.read_text('utf-8')) if path.exists() else {}
    if journal.get('status') in ('running','failed') and not journal.get('ai_link'):return False
    return bool(vault.data.get('ai_installed') or journal.get('status')=='complete' or journal.get('ai_link'))
