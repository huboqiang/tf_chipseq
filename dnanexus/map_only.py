#!/usr/bin/env python

import os.path, sys, subprocess, logging, re, json, urlparse, requests
import dxpy

EPILOG = '''Notes:

Examples:

	%(prog)s
'''

DEFAULT_APPLET_PROJECT = 'E3 ChIP-seq'
INPUT_SHIELD_APPLET_NAME = 'input_shield'
MAPPING_APPLET_NAME = 'encode_bwa'
FILTER_QC_APPLET_NAME = 'filter_qc'
XCOR_APPLET_NAME = 'xcor'
POOL_APPLET_NAME = 'pool'
REFERENCES = [
	{'build': 'GRCh38', 'organism': 'human', 'sex': 'male',   'file': 'ENCODE Reference Files:/GRCh38/GRCh38_minimal_XY.tar.gz'},
	{'build': 'GRCh38', 'organism': 'human', 'sex': 'female', 'file': 'ENCODE Reference Files:/GRCh38/GRCh38_minimal_X.tar.gz'},
	{'build': 'mm10',   'organism': 'mouse', 'sex': 'male',   'file': 'ENCODE Reference Files:/mm10/male.mm10.tar.gz'},
	{'build': 'mm10',   'organism': 'mouse', 'sex': 'female', 'file': 'ENCODE Reference Files:/mm10/female.mm10.tar.gz'},
	{'build': 'hg19',   'organism': 'human', 'sex': 'male',   'file': 'ENCODE Reference Files:/hg19/hg19_XY.tar.gz'},
	{'build': 'hg19',   'organism': 'human', 'sex': 'female', 'file': 'ENCODE Reference Files:/hg19/hg19_X.tar.gz'}
	]
KEYFILE = os.path.expanduser("~/keypairs.json")
DEFAULT_SERVER = 'https://www.encodeproject.org'
DNANEXUS_ENCODE_SNAPSHOT = 'ENCODE-SDSC-snapshot-20140505'

APPLETS = {}

def get_args():
	import argparse
	parser = argparse.ArgumentParser(
		description=__doc__, epilog=EPILOG,
		formatter_class=argparse.RawDescriptionHelpFormatter)

	parser.add_argument('infile', nargs='?', type=argparse.FileType('r'), default=sys.stdin)
	parser.add_argument('outfile', nargs='?', type=argparse.FileType('w'), default=sys.stdout)
	parser.add_argument('--build', help="Genome build, e.g. GRCh38, hg19, or mm10")
	parser.add_argument('--debug',   help="Print debug messages", 				default=False, action='store_true')
	parser.add_argument('--outp',    help="Output project name or ID", 			default=dxpy.WORKSPACE_ID)
	parser.add_argument('--outf',    help="Output folder name or ID", 			default="/")
	parser.add_argument('--applets', help="Name of project containing applets", default=DEFAULT_APPLET_PROJECT)
	#parser.add_argument('--instance_type', help="Instance type for mapping",	default=None)
	parser.add_argument('--key', help="The keypair identifier from the keyfile.  Default is --key=default",
		default='default')
	parser.add_argument('--yes',   help="Run the workflows created", 			default=False, action='store_true')
	parser.add_argument('--raw',   help="Produce only raw (unfiltered) bams", default=False, action='store_true')
	parser.add_argument('--tag',   help="String to add to the workflow name", default="")

	args = parser.parse_args()

	if args.debug:
		logging.basicConfig(format='%(levelname)s:%(message)s', level=logging.DEBUG)
	else: #use the defaulf logging level
		logging.basicConfig(format='%(levelname)s:%(message)s')

	return args

def processkey(key):

    if key:
        keysf = open(KEYFILE,'r')
        keys_json_string = keysf.read()
        keysf.close()
        keys = json.loads(keys_json_string)
        key_dict = keys[key]
    else:
        key_dict = {}
    AUTHID = key_dict.get('key')
    AUTHPW = key_dict.get('secret')
    if key:
        SERVER = key_dict.get('server')
    else:
        SERVER = DEFAULT_SERVER

    if not SERVER.endswith("/"):
        SERVER += "/"

    return (AUTHID,AUTHPW,SERVER)

def encoded_get(url, keypair=None):
    HEADERS = {'content-type': 'application/json'}
    url = urlparse.urljoin(url,'?format=json&frame=embedded&datastore=database')
    if keypair:
        response = requests.get(url, auth=keypair, headers=HEADERS)
    else:
        response = requests.get(url, headers=HEADERS)
    return response.json()

def resolve_project(identifier, privs='r'):
	project = dxpy.find_one_project(name=identifier, level='VIEW', name_mode='exact', return_handler=True, zero_ok=True)
	if project == None:
		try:
			project = dxpy.get_handler(identifier)
		except:
			logging.error('Could not find a unique project with name or id %s' %(identifier))
			raise ValueError(identifier)
	logging.debug('Project %s access level is %s' %(project.name, project.describe()['level']))
	if privs == 'w' and project.describe()['level'] == 'VIEW':
		logging.error('Output project %s is read-only' %(identifier))
		raise ValueError(identifier)
	return project

def resolve_folder(project, identifier):
	if not identifier.startswith('/'):
		identifier = '/' + identifier
	try:
		project_id = project.list_folder(identifier)
	except:
		try:
			project_id = project.new_folder(identifier, parents=True)
		except:
			logging.error("Cannot create folder %s in project %s" %(identifier, project.name))
			raise ValueError('%s:%s' %(project.name, identifier))
		else:
			logging.info("New folder %s created in project %s" %(identifier, project.name))
	return identifier

def find_applet_by_name(applet_name, applets_project_id):
	'''Looks up an applet by name in the project that holds tools.  From Joe Dale's code.'''
	cached = '*'
	if (applet_name, applets_project_id) not in APPLETS:
		found = dxpy.find_one_data_object(classname="applet", name=applet_name,
										  project=applets_project_id,
										  zero_ok=False, more_ok=False, return_handler=True)
		APPLETS[(applet_name, applets_project_id)] = found
		cached = ''

	logging.info(cached + "Resolved applet %s to %s" %(applet_name, APPLETS[(applet_name, applets_project_id)].get_id()))
	return APPLETS[(applet_name, applets_project_id)]

def filenames_in(files=None):
	if not len(files):
		return []
	else:
		return [f.get('submitted_file_name') for f in files]

def files_to_map(exp_obj):
	if not exp_obj or not exp_obj.get('files'):
		return []
	else:
		files = []
		for file_obj in exp_obj.get('files'):
			if file_obj.get('output_type') == 'reads' and \
				file_obj.get('file_format') == 'fastq' and \
				file_obj.get('replicate') and \
				file_obj.get('submitted_file_name') not in filenames_in(files):
			  	files.extend([file_obj])
			elif file_obj.get('submitted_file_name') in filenames_in(files):
				logging.warning('%s:%s Duplicate filename, ignoring.' %(exp_obj.get('accession'),file_obj.get('accession')))
			elif file_obj.get('output_type') == 'reads' and \
				file_obj.get('file_format') == 'fastq' and not file_obj.get('replicate'):
				logging.warning('%s: Fastq has no replicate' %(file_obj.get('accession')))
		return files

def replicates_to_map(files):
	if not files:
		return []
	else:
		return [f.get('replicate') for f in files]

def choose_reference(experiment, biorep_n):

	try:
		replicate = next(rep for rep in experiment.get('replicates') if rep.get('biological_replicate_number') == biorep_n)
		logging.debug('Replicate uuid %s' %(replicate.get('uuid')))
	except:
		logging.error('%s cannot resolve biological_replicate_number %s' %(experiment.get('accession'), biorep_n))
		return None

	try:
		organism_name = replicate.get('library').get('biosample').get('organism').get('name')
		logging.debug("Organism name %s" %(organism_name))
	except:
		logging.error('%s:rep%d Cannot determine organism.' %(experiment.get('accession'), biorep_n))
		return None

	try:
		sex = replicate.get('library').get('biosample').get('sex')
		if sex not in ['male', 'female']:
			raise
	except:
		logging.warning('%s:rep%d Sex is %s.  Mapping to male reference.' %(experiment.get('accession'), biorep_n, sex))
		sex = 'male'

	logging.debug('Organism %s sex %s' %(organism_name, sex))
	genome_build = args.build

	reference = next((ref.get('file') for ref in REFERENCES if ref.get('organism') == organism_name and ref.get('sex') == sex and ref.get('build') == genome_build), None)
	logging.debug('Found reference %s' %(reference))
	return reference

def build_workflow(experiment, biorep_n, input_shield_stage_input, key):

	output_project = resolve_project(args.outp, 'w')
	logging.debug('Found output project %s' %(output_project.name))

	applet_project = resolve_project(args.applets, 'r')
	logging.debug('Found applet project %s' %(applet_project.name))

	mapping_applet = find_applet_by_name(MAPPING_APPLET_NAME, applet_project.get_id())
	logging.debug('Found applet %s' %(mapping_applet.name))

	input_shield_applet = find_applet_by_name(INPUT_SHIELD_APPLET_NAME, applet_project.get_id())
	logging.debug('Found applet %s' %(input_shield_applet.name))

	workflow_output_folder = resolve_folder(output_project, args.outf + '/workflows/' + experiment.get('accession') + '/' + 'rep%d' %(biorep_n))

	fastq_output_folder = resolve_folder(output_project, args.outf + '/fastqs/' + experiment.get('accession') + '/' + 'rep%d' %(biorep_n))
	mapping_output_folder = resolve_folder(output_project, args.outf + '/raw_bams/' + experiment.get('accession') + '/' + 'rep%d' %(biorep_n))

	if args.raw:
		workflow_title = 'Map %s rep%d to %s (no filter)' %(experiment.get('accession'), biorep_n, args.build)
		workflow_name = 'ENCODE raw mapping pipeline'
	else:
		workflow_title = 'Map %s rep%d to %s and filter' %(experiment.get('accession'), biorep_n, args.build)
		workflow_name = 'ENCODE mapping pipeline'

	if args.tag:
		workflow_title += ': %s' %(args.tag)

	workflow = dxpy.new_dxworkflow(
		title=workflow_title,
		name=workflow_name,
		project=output_project.get_id(),
		folder=workflow_output_folder
	)

	input_shield_stage_id = workflow.add_stage(
		input_shield_applet,
		name='Gather inputs %s rep%d' %(experiment.get('accession'), biorep_n),
		folder=fastq_output_folder,
		stage_input=input_shield_stage_input
	)
	
	mapping_stage_id = workflow.add_stage(
		mapping_applet,
		name='Map %s rep%d' %(experiment.get('accession'), biorep_n),
		folder=mapping_output_folder,
		stage_input={'input_JSON': dxpy.dxlink({'stage': input_shield_stage_id, 'outputField': 'output_JSON'})}
	)

	if not args.raw:
		final_output_folder = resolve_folder(output_project, args.outf + '/bams/' + experiment.get('accession') + '/' + 'rep%d' %(biorep_n))

		filter_qc_applet = find_applet_by_name(FILTER_QC_APPLET_NAME, applet_project.get_id())
		logging.debug('Found applet %s' %(filter_qc_applet.name))

		filter_qc_stage_id = workflow.add_stage(
			filter_qc_applet,
			name='Filter and QC %s rep%d' %(experiment.get('accession'), biorep_n),
			folder=final_output_folder,
			stage_input={
				'input_bam': dxpy.dxlink({'stage': mapping_stage_id, 'outputField': 'mapped_reads'}),
				'paired_end': dxpy.dxlink({'stage': mapping_stage_id, 'outputField': 'paired_end'})
			}
		)

		xcor_applet = find_applet_by_name(XCOR_APPLET_NAME, applet_project.get_id())
		logging.debug('Found applet %s' %(xcor_applet.name))

		xcor_stage_id = workflow.add_stage(
			xcor_applet,
			name='Calculate cross-correlation %s rep%d' %(experiment.get('accession'), biorep_n),
			folder=final_output_folder,
			stage_input={
				'input_bam': dxpy.dxlink({'stage': filter_qc_stage_id, 'outputField': 'filtered_bam'}),
				'paired_end': dxpy.dxlink({'stage': filter_qc_stage_id, 'outputField': 'paired_end'})
			}
		)


	''' This should all be done in the shield's postprocess entrypoint
	if args.accession_outputs:
		derived_from = input_shield_stage_input.get('reads1')
		if reads2:
			derived_from.append(reads2)
		files_json = {dxpy.dxlink({'stage': mapping_stage_id, 'outputField': 'mapped_reads'}) : {
			'notes': 'Biorep%d | Mapped to %s' %(biorep_n, input_shield_stage_input.get('reference_tar')),
			'lab': 'j-michael-cherry',
			'award': 'U41HG006992',
			'submitted_by': 'jseth@stanford.edu',
			'file_format': 'bam',
			'output_type': 'alignments',
			'derived_from': derived_from,
			'dataset': experiment.get('accession')}
		}
		output_shield_stage_id = workflow.add_stage(
			output_shield_applet,
			name='Accession outputs %s rep%d' %(experiment.get('accession'), biorep_n),
			folder=mapping_output_folder,
			stage_input={'files': [dxpy.dxlink({'stage': mapping_stage_id, 'outputField': 'mapped_reads'})],
						 'files_json': files_json,
						 'key': input_shield_stage_input.get('key')}
		)
	'''
	return workflow

def map_only(experiment, biorep_n, files, key):

	if not files:
		logging.debug('%s:%s No files to map' %(experiment.get('accession'), biorep_n))
		return
	#look into the structure of files parameter to decide on pooling, paired end etc.

	workflows = []
	input_shield_stage_input = {}

	reference_tar = choose_reference(experiment, biorep_n)
	if not reference_tar:
		logging.warning('%s:%s Cannot determine reference' %(experiment.get('accession'), biorep_n))
		return

	input_shield_stage_input.update({
		'reference_tar' : reference_tar,
		'debug': args.debug,
		'key': key
	})

	if all(isinstance(f, dict) for f in files): #single end
		input_shield_stage_input.update({'reads1': [f.get('accession') for f in files]})
		workflows.append(build_workflow(experiment, biorep_n, input_shield_stage_input, key))
	elif all(isinstance(f, tuple) for f in files): #paired-end
		for readpair in files:
			input_shield_stage_input.update(
				{'reads1': [next(f.get('accession') for f in readpair if f.get('paired_end') == '1')],
				 'reads2': next(f.get('accession') for f in readpair if f.get('paired_end') == '2')})
			workflows.append(build_workflow(experiment, biorep_n, input_shield_stage_input, key))
	else:
		logging.error('%s: List of files to map appears to be mixed single-end and paired-end: %s' %(experiment.get('accession'), files))

	jobs = []
	if args.yes:
		for wf in workflows:
			jobs.append(wf.run({}))
	return jobs

def main():
	global args
	args = get_args()

	authid, authpw, server = processkey(args.key)
	keypair = (authid,authpw)


	for exp_id in args.infile:
		outstrings = []
		encode_url = urlparse.urljoin(server,exp_id.rstrip())
		experiment = encoded_get(encode_url, keypair)
		outstrings.append(exp_id.rstrip())
		files = files_to_map(experiment)
		outstrings.append(str(len(files)))
		outstrings.append(str([f.get('accession') for f in files]))
		replicates = replicates_to_map(files)

		if files:
			for biorep_n in set([rep.get('biological_replicate_number') for rep in replicates]):
				outstrings.append('rep%s' %(biorep_n))
				biorep_files = [f for f in files if f.get('replicate').get('biological_replicate_number') == biorep_n]
				paired_files = []
				unpaired_files = []
				while biorep_files:
					file_object = biorep_files.pop()
					if file_object.get('paired_end') == None: # group all the unpaired reads for this biorep together
						unpaired_files.append(file_object)
					elif file_object.get('paired_end') in ['1','2']:
						if file_object.get('paired_with'):
							mate = next((f for f in biorep_files if f.get('@id') == file_object.get('paired_with')), None)
						else: #have to find the file that is paired with this one
							mate = next((f for f in biorep_files if f.get('paired_with') == file_object.get('@id')), None)
						if mate:
							biorep_files.remove(mate)
						else:
							logging.warning('%s:%s could not find mate' %(experiment.get('accession'), file_object.get('accession')))
							mate = {}
						paired_files.append((file_object,mate))
				if biorep_files:
					logging.warning('%s: leftover file(s) %s' %(experiment.get('accession'), biorep_files))
				if paired_files:
					pe_jobs = map_only(experiment, biorep_n, paired_files, args.key)
				if unpaired_files:
					se_jobs = map_only(experiment, biorep_n, unpaired_files, args.key)
				if paired_files and pe_jobs:
					outstrings.append('paired:%s' %([(a.get('accession'), b.get('accession')) for (a,b) in paired_files]))
					outstrings.append('paired jobs:%s' %([j.get_id() for j in pe_jobs]))
				else:
					outstrings.append('paired:%s' %(None))
				if unpaired_files and se_jobs:
					outstrings.append('unpaired:%s' %([f.get('accession') for f in unpaired_files]))
					outstrings.append('unpaired jobs:%s' %([j.get_id() for j in se_jobs]))
				else:
					outstrings.append('unpaired:%s' %(None))

			print '\t'.join(outstrings)
		else: # no files
			if not replicates:
				logging.warning('%s: No files and no replicates' %experiment.get('accession'))
			else:
				logging.warning('%s: No files to map' %experiment.get('accession'))
		if files and not replicates:
			logging.warning('%s: Files but no replicates' %experiment.get('accession'))

if __name__ == '__main__':
	main()
