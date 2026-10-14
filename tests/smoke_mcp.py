from pathlib import Path
"""Run a real stdio MCP client against a temporary synthetic-mail database."""
import asyncio
import os
import sys
import tempfile

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ou_email_agent.mailbox import parse_message
from ou_email_agent.store import Store


async def main():
    with tempfile.TemporaryDirectory() as directory:
        db=Store(directory)
        raw=b'From: test@example.org\r\nSubject: Synthetic MCP test\r\nMessage-ID: <mcp@test>\r\n\r\nA synthetic test message.'
        db.insert(parse_message(raw),'INBOX','1',1,True)
        db.close()
        params=StdioServerParameters(command=sys.executable,
                                     args=[str(Path(__file__).resolve().parents[1] / 'launch-mcp.py')],
                                     cwd=directory,
                                     env={**os.environ,'OU_EMAIL_DATA':directory,'OU_EMAIL_CONFIG':str(Path(__file__).resolve().parents[1] / 'config.example.json')})
        async with stdio_client(params) as (read,write):
            async with ClientSession(read,write) as session:
                await session.initialize()
                names={tool.name for tool in (await session.list_tools()).tools}
                assert {'email_status','search_email','get_email','get_email_thread',
                        'list_important_email','list_agenda_changes','acknowledge_agenda_change'}<=names,names
                search_tool=next(t for t in (await session.list_tools()).tools if t.name=='search_email')
                assert 'recipient' in search_tool.inputSchema['properties']
                for name,args in [('email_status',{}),('get_email',{'message_id':1}),
                                  ('search_email',{'query':'','recipient':'nobody@example.org'}),
                                  ('get_email_thread',{'message_id':1}),('list_agenda_changes',{})]:
                    result=await session.call_tool(name,args)
                    assert not result.isError,result
                bad=await session.call_tool('acknowledge_agenda_change',{
                    'item_id':'nonexistent','destination':'calendar','status':'done','reference':'test'})
                assert bad.isError
                print('MCP stdio integration passed: 7 tools registered, reads succeeded, invalid acknowledgement rejected.')


if __name__=='__main__': asyncio.run(main())
