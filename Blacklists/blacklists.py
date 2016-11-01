import socket
import ipaddress
import re
import json
import time
from flask.json import jsonify
from flask import url_for, render_template
from celery import Celery
import flask
import httplib2
import base64
import email
import uuid

from apiclient import discovery, errors
from oauth2client import client

app = flask.Flask(__name__)
app.config['CELERY_BROKER_URL'] = 'redis://localhost:6379/0'
app.config['CELERY_RESULT_BACKEND'] ='redis://localhost:6379/0'
app.secret_key = str(uuid.uuid4())

celery = Celery(app.name, broker=app.config['CELERY_BROKER_URL'])
celery.conf.update(app.config)


@app.route('/')
def index():
    if 'credentials' not in flask.session:
        return flask.redirect(flask.url_for('oauth2callback'))
    credentials = client.OAuth2Credentials.from_json(flask.session['credentials'])
    if credentials.access_token_expired:
        return flask.redirect(flask.url_for('oauth2callback'))
    else:
        http_auth = credentials.authorize(httplib2.Http())
        gmail_service = discovery.build('gmail', 'v1', http_auth)
        threads = gmail_service.users().threads().list(userId='me').execute()
        return json.dumps(threads)


@celery.task()
@app.route('/oauth2callback')
def oauth2callback():
    #Read only Access to Gmail
    flow = client.flow_from_clientsecrets(
        '/var/www/Blacklists/Blacklists/client_secrets.json',
        scope='https://www.googleapis.com/auth/gmail.readonly',
        redirect_uri=flask.url_for('oauth2callback', _external=True)
    )
    flow.params['approval_prompt'] = 'force'
    flow.params['access_type'] = 'offline'
    if 'code' not in flask.request.args:
        auth_uri = flow.step1_get_authorize_url()
        return flask.redirect(auth_uri)
    else:
        auth_code = flask.request.args.get('code')
        credentials = flow.step2_exchange(auth_code)
        flask.session['credentials'] = credentials.to_json()
        return flask.redirect(flask.url_for('getmail'))



@celery.task(bind=True)
def process_task(self,credentials):
 with app.app_context():
        credentials = client.OAuth2Credentials.from_json(credentials)
        http_auth = credentials.authorize(httplib2.Http())
        gmail_service = discovery.build('gmail', 'v1', http_auth)

	#Query to obtain emails from Jan 1,2016 to Aug 31,2016 
        query = 'in:all after:2016/01/01 before:2016/08/31'
        try:
            response = gmail_service.users().messages().list(userId='me', q=query).execute()
            messages = []
	    #Obtaining all Email IDs 
            if 'messages' in response:
                messages.extend(response['messages'])
            while 'nextPageToken' in response:
                page_token = response['nextPageToken']
                response = gmail_service.users().messages().list(userId='me', q=query, pageToken=page_token).execute()
                messages.extend(response['messages'])
	    #Writing to file based on the task identifier generated internally. Note that there is no link the user contributing
	    output_file_handler = open(+str(celery.current_task.request.id), 'w')
	    done=0
            total=len(messages)
	    d={}
            for item in messages:
		 done=done+1	
		 if done%1000==0:
			print "Refreshing tokens"
			credentials.refresh(http_auth)
		 try:
                 	message = gmail_service.users().messages().get(userId='me', id=item['id']).execute()
   	         except errors.HttpError, error:
			print "INTERNAL ERROR",error
			print "Sleeping for sometime"
			time.sleep(10)
			continue
		 temp_ip_candidates = re.findall(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b", str(message["payload"]["headers"]))
	         ip_candidates=[]
		 for ip in temp_ip_candidates:
			if ipaddress.ip_address(ip).is_private==False:
				ip_candidates.append(ip)
		 if "headers" not in message["payload"]:
			continue
		 #Obtaining the type of Email
		 labels=message["labelIds"]
		 label="Ham"
		 if "INBOX" in labels:
			label="Ham"
		 if "SPAM" in labels:
			label="Spam"
		 t=time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(float(message["internalDate"][:-3])))
		 for ip in ip_candidates:
			 if ip not in d:
				d[ip]=[ip,label,1,t]
			 else:
                                old_count=d[ip][2]
				d[ip]=[ip,label,old_count+1,t]
		 self.update_state(state='PROGRESS', 
                          meta={'current': done, 'total': total,'data':d})		

		 #Writing IP address, type of email and tomestamp of email to file
           	 output_file_handler.write(json.dumps(ip_candidates)+"qwerty123"+str(label)+"qwerty123"+str(message["internalDate"])+"\n")
	    output_file_handler.close()
	    return {'current': done, 'total': total,'result': 42,'data':d}
        except errors.HttpError, error:
            print 'An error occurred: %s' % error





@app.route('/getmail')
def getmail(): 
    credentials=flask.session['credentials']
    task = process_task.delay(credentials)
    return render_template('index.html',name=json.dumps({"url":"/status/"+str(task.id)}))


@app.route('/status/<task_id>')
def taskstatus(task_id):
    task = process_task.AsyncResult(task_id)
    if task.state == 'PENDING':
        response = {
            'state': task.state,
            'current': 0,
            'total': 1,
            'status': 'Obtaining all Mails from Jan 1st,2016'
        }
    elif task.state != 'FAILURE':
        response = {
            'state': task.state,
            'current': task.info.get('current', 0),
            'total': task.info.get('total', 1),
            'status': task.info.get('status', '')
        }
        if 'result' in task.info:
            response['result'] = task.info['result']
	if 'data' in task.info:
	    response['data']=task.info['data']

    else:
        response = {
            'state': task.state,
            'current': 1,
            'total': 1,
            'status': str(task.info),  # this is the exception raised
        }
    return jsonify(response)


if __name__ == '__main__':
    app.debug = True
    app.run()

