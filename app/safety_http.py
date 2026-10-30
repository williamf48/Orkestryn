"""Small Safety Lab API adapter using the existing account and CSRF boundary."""
from urllib.parse import parse_qs,urlsplit
import safety_environment as env
import safety_lab as research
import safety_live
import safety_ui


def query(handler,field):
    value=parse_qs(urlsplit(handler.path).query,keep_blank_values=True,max_num_fields=1)
    if set(value)!={field} or len(value[field])!=1 or not value[field][0]:
        raise ValueError('Choose one exact research '+field+'.')
    return value[field][0]


def get(handler,path,auth):
    if path=='/safety':
        handler.reply(200,safety_ui.PAGE,html=True);return True
    if path not in {'/api/safety','/api/safety/case','/api/safety/report','/safety/report','/api/safety/evidence'}:
        return False
    principal,csrf=handler.auth(auth)
    with research.Lab(handler.server.root) as lab:
        if path=='/api/safety':
            if urlsplit(handler.path).query:raise ValueError('The research overview has no query options.')
            handler.reply(200,{'principal':principal,'csrf':csrf,'model_requests_enabled':handler.server.allow_browser_keys,**lab.state()})
        elif path=='/api/safety/case':
            case=lab.case(query(handler,'id'))
            handler.reply(200,{'id':case['id'],'public_context':env.public_context(case),'view_digest':env.digest(env.public_context(case)),
                'scope':'This monitor projection excludes evaluator labels, private attack goals and oracle results.'})
        else:
            run=lab.run(query(handler,'run'))
            if path=='/api/safety/report':handler.reply(200,run)
            elif path=='/safety/report':handler.reply(200,research.render(run),html=True)
            else:handler.reply_archive(*research.export_bytes(run))
    return True


def post(handler,endpoint,body,principal,auth):
    if endpoint!='/api/safety':return False
    auth.require(principal,'manager')
    action=body.get('action')
    allowed={
        'evaluate':({'action'},{'split','budgets','case_id','framing'}),
        'import-candidate':({'action','base_id','candidate'},{'model'}),
        'paid-review':({'action','case_id','kind','output_cap'},{'visibility'}),
        'paid-attempt':({'action','case_id','kind','output_cap','review_digest','confirmed','nebius_api_key'},{'visibility'}),
    }
    if action not in allowed:raise ValueError('Choose a registered research operation.')
    required,optional=allowed[action]
    if not required.issubset(body) or set(body)-required-optional:raise ValueError('Use the exact research operation fields.')
    with research.Lab(handler.server.root) as lab:
        if action=='evaluate':
            value=lab.evaluate(split=body.get('split','development'),budgets=body.get('budgets',research.DEFAULT_BUDGETS),
                               case_id=body.get('case_id'),principal=principal,framing=body.get('framing',False))
        elif action=='import-candidate':
            case=lab.import_candidate(body['base_id'],body['candidate'],body.get('model','not reported'))
            value={'status':'SAVED','case_id':case['id'],'model_requests':0,'source':'IMPORTED_UNVERIFIED'}
        elif action=='paid-review':
            if not handler.server.allow_browser_keys:raise ValueError('This workspace disables entered-key model experiments.')
            value=safety_live.preview(lab,body['case_id'],body['kind'],body['output_cap'],body.get('visibility','full_context'))
        else:
            if not handler.server.allow_browser_keys:raise ValueError('This workspace disables entered-key model experiments.')
            value=safety_live.attempt(lab,body['case_id'],body['kind'],body['output_cap'],body['review_digest'],
                                     body['nebius_api_key'],confirmed=body['confirmed'],principal=principal,visibility=body.get('visibility','full_context'))
        handler.reply(200,value)
    return True
