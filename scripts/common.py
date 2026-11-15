from pathlib import Path
import getpass,json,os
ROOT=Path(__file__).resolve().parent.parent
DEFAULTS={'region':'EUW','locale':'en_GB'}
# Region codes the launcher and scripts/installer.sh accept: the names the installer shows, plus
# the platform codes installer.sh maps LAN/LAS/OCE to. Keep the names in step with REGIONS in
# scripts/install-native.sh.
REGIONS=('EUW','EUNE','NA','BR','KR','JP','OCE','LAN','LAS','TR','RU','LA1','LA2','OC1')
# Locales the Riot Client ships. Keep in step with LOCALES in scripts/install-native.sh.
LOCALES=('ar_AE','cs_CZ','de_DE','el_GR','en_AU','en_GB','en_PH','en_SG','en_US','es_AR','es_ES',
         'es_MX','fr_FR','hu_HU','id_ID','it_IT','ja_JP','ko_KR','pl_PL','pt_BR','ro_RO','ru_RU',
         'th_TH','tr_TR','vi_VN','zh_CN','zh_MY','zh_TW')

def configuration():
    file=ROOT/'build/config.json'
    if file.exists():c=json.loads(file.read_text())
    else:
        username=getpass.getuser()
        c={'project_root':str(ROOT),'host_home':str(Path.home()),'prefix':str(Path.home()/'.darling'),'guest_user':username,'guest_home':'/Users/'+username,'sdk':'/usr/local/libexec/darling'}
    for key,value in DEFAULTS.items():c.setdefault(key,value)
    return c

def render(text,config=None):
    c=config or configuration()
    for token,key in [('PROJECT_ROOT','project_root'),('HOST_HOME','host_home'),('DPREFIX','prefix'),('GUEST_USER','guest_user'),('GUEST_HOME','guest_home'),('DARLING_SDK','sdk'),('REGION','region'),('LOCALE','locale')]:
        text=text.replace('@'+token+'@',c[key])
    return text
