"""Validate split AI packaging, including optional AI and hostile/corrupt inputs."""
import json
import zipfile
import pytest
from prodcast_manager.release import Release, sha
from prodcast_manager.offline import PROFILE


@pytest.fixture(params=['saatchi190499', 'byteall-energy'])
def bundle(tmp_path, request):
    root=tmp_path/'complete';root.mkdir()
    artifacts=[]
    for key in ['app-deployment','backend-linux-amd64','frontend-linux-amd64','gateway-linux-amd64','ai-deployment','ai-linux-amd64','worker-windows-amd64']:
        comp,suffix=key.split('-',1)
        name=f'prodcast-{comp}-v0.4-rc.1-{suffix}'+('.zip' if comp=='worker' else '.tar.gz')
        path=root/name;path.write_bytes(b'component')
        artifacts.append({'name':name,'bytes':path.stat().st_size,'sha256':sha(path)})
    for name in ['deps.tar','db.tar','runtime.tar.xz','metadata.tar.gz']:
        path=root/name;path.write_bytes(name.encode())
        artifacts.append({'name':name,'bytes':path.stat().st_size,'sha256':sha(path)})
    archive=tmp_path/'ollama.zip'
    with zipfile.ZipFile(archive,'w') as z:
        for name in ['runtime.tar.xz','metadata.tar.gz']:z.write(root/name,name)
    for name in ['runtime.tar.xz','metadata.tar.gz']:(root/name).unlink()
    model=tmp_path/'model.gguf';model.write_bytes(b'model')
    spec={'schema':1,'linux_packages':{'ubuntu:22.04':'deps.tar'},'db_images':'db.tar',
          'ollama_runtime':'runtime.tar.xz','model_archive':'metadata.tar.gz','model_name':'qwen3:4b-instruct',
          'model_digest':'sha256:'+'a'*64,'external_model':{'sha256':sha(model),'bytes':model.stat().st_size},
          'external_ollama':{'name':archive.name,'bytes':archive.stat().st_size,'sha256':sha(archive)}}
    contract={'schema':3,'profile':PROFILE,'minimum_manager':'0.3.2','python':'3.14.7','database_major':18,
              'migration_policy':'forward-only','upgrade_from':['v0.3'],'offline':spec}
    components=[{'name':'prodcast-'+n,'images':[{'name':'prodcast-'+n,'transport_tag':'ghcr.io/'+request.param+'/prodcast-'+n+':test'}]} for n in ['backend','frontend','gateway','ai']]
    doc={'version':'v0.4-rc.1','management':contract,'components':components,'artifacts':artifacts}
    def load(ai=False,ollama=archive,model_file=model):
        manifest=root/'release-manifest.json';manifest.write_text(json.dumps(doc))
        return Release(root,tmp_path/'cache',sha(manifest),validate_ai=ai,ollama_path=ollama,model_path=model_file)
    return root,archive,model,doc,load


def test_core_install_does_not_require_either_ai_file(bundle):
    root,archive,model,doc,load=bundle
    r=load(ollama='',model_file='')
    assert r.for_role('app') and r.for_role('db') and r.for_role('worker1')
    with pytest.raises(ValueError,match='Select the Ollama components'):r.for_role('ai')


def test_ai_uses_two_selected_files_and_keeps_core_directory_unchanged(bundle):
    root,archive,model,doc,load=bundle
    r=load(ai=True)
    payload=r.for_role('ai')
    assert payload['offline-model_blob']==model
    assert payload['offline-ollama_runtime'].read_bytes()==b'runtime.tar.xz'
    assert payload['offline-model_archive'].read_bytes()==b'metadata.tar.gz'
    assert not (root/'runtime.tar.xz').exists()


def test_ai_can_be_added_later_with_same_release(bundle):
    root,archive,model,doc,load=bundle
    r=load(ollama='',model_file='');digest=r.digest
    r.for_role('db');r.ollama_path=archive;r.model_path=model
    assert r.for_role('ai')['offline-model_blob']==model and r.digest==digest


def test_wrong_archive_is_rejected_before_extraction(bundle):
    root,archive,model,doc,load=bundle
    r=load();archive.write_bytes(b'wrong file')
    assert r.for_role('app')
    with pytest.raises(ValueError,match='Ollama components checksum mismatch'):r.for_role('ai')


def test_model_is_still_required_and_checked(bundle):
    root,archive,model,doc,load=bundle
    r=load(model_file='')
    with pytest.raises(ValueError,match='Select the separately'):r.for_role('ai')
    r.model_path=model;model.write_bytes(b'wrong')
    with pytest.raises(ValueError,match='AI model checksum mismatch'):r.for_role('ai')


@pytest.mark.parametrize('name',['../escape','extra.txt','app.env'])
def test_bundle_cannot_add_or_override_other_files(bundle,name):
    root,archive,model,doc,load=bundle
    with zipfile.ZipFile(archive,'a') as z:z.writestr(name,b'bad')
    doc['management']['offline']['external_ollama'].update(bytes=archive.stat().st_size,sha256=sha(archive))
    r=load()
    with pytest.raises(ValueError,match='Unexpected files'):r.for_role('ai')


def test_corrupt_extracted_cache_is_rejected(bundle):
    root,archive,model,doc,load=bundle
    r=load();payload=r.for_role('ai');payload['offline-ollama_runtime'].write_bytes(b'changed')
    with pytest.raises(ValueError,match='Missing/corrupt AI offline payload'):r.for_role('ai')


def test_inner_file_must_match_manifest_even_when_zip_matches(bundle):
    root,archive,model,doc,load=bundle
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('runtime.tar.xz',b'bad runtime');z.writestr('metadata.tar.gz',b'metadata.tar.gz')
    doc['management']['offline']['external_ollama'].update(bytes=archive.stat().st_size,sha256=sha(archive))
    with pytest.raises(ValueError,match='Missing/corrupt AI offline payload'):load(ai=True)


def test_old_names_and_recompressed_zip_use_trusted_inner_bytes(bundle):
    root,archive,model,doc,load=bundle
    old_hash=doc['management']['offline']['external_ollama']['sha256']
    with zipfile.ZipFile(archive,'w',compression=zipfile.ZIP_DEFLATED) as z:
        z.writestr('ollama-v0.4-runtime.tar.xz',b'runtime.tar.xz')
        z.writestr('ollama-v0.4-model.tar.gz',b'metadata.tar.gz')
        z.comment=b'older release packaging'
    assert sha(archive)!=old_hash
    files=load(ai=True).for_role('ai')
    assert files['offline-ollama_runtime'].name=='runtime.tar.xz'
    assert files['offline-model_archive'].read_bytes()==b'metadata.tar.gz'


@pytest.mark.parametrize('name',['../runtime.tar.xz','/runtime.tar.xz','C:runtime.tar.xz'])
def test_compatible_bundle_still_rejects_unsafe_names(bundle,name):
    root,archive,model,doc,load=bundle
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr(name,b'runtime.tar.xz');z.writestr('metadata.tar.gz',b'metadata.tar.gz')
    with pytest.raises(ValueError):load(ai=True)


def test_same_size_changed_inner_bytes_are_rejected(bundle):
    root,archive,model,doc,load=bundle
    with zipfile.ZipFile(archive,'w') as z:
        z.writestr('old-runtime.tar.xz',b'Xuntime.tar.xz')
        z.writestr('metadata.tar.gz',b'metadata.tar.gz')
    with pytest.raises(ValueError,match='Missing/corrupt AI offline payload'):load(ai=True)


@pytest.mark.parametrize('owner', ['untrusted', 'byteall-energy.evil'])
def test_rejects_untrusted_image_owner(bundle, owner):
    root, archive, model, doc, load = bundle
    doc['components'][0]['images'][0]['transport_tag'] = f'ghcr.io/{owner}/prodcast-backend:test'
    with pytest.raises(ValueError, match='Unexpected image reference'):
        load()
