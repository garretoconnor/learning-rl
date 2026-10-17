"""Private controls protect behavior outside the selected repair scopes."""
SOURCE = '''import errno,io,os,unittest,click
class CompatibilityControls(unittest.TestCase):
 def test_existing_secho_keyword(self):
  output=io.StringIO();click.secho(text='hello',file=output,color=False)
  self.assertEqual(output.getvalue(),'hello\\n')
 def test_nonstandalone_pipe_still_propagates(self):
  readfd,writefd=os.pipe();os.close(readfd);output=os.fdopen(writefd,'w')
  @click.command()
  def cli():click.echo('payload',file=output)
  try:
   with self.assertRaises(BrokenPipeError):cli.main(args=[],prog_name='cli',standalone_mode=False)
  finally:
   try:output.close()
   except BrokenPipeError:pass
 def test_disabled_help_hint(self):
  for command,options in [(click.Command('nohelp',add_help_option=False),{}),(click.Command('empty'),{'help_option_names':[]})]:
   context=click.Context(command,info_name='app',**options)
   output=io.StringIO();click.UsageError('bad',ctx=context).show(file=output)
   self.assertIn('Error: bad',output.getvalue())
   self.assertNotIn('Try "',output.getvalue())
'''
