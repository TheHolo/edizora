from app.domain.enums import MediaType
from app.domain.models import PreparedMedia
from app.storage.assets import safe_path


def load_prepared_media(plan, links, post, root, mode):
    manifest=plan.get('media')
    prepared=[]
    if manifest is not None:
        if len(manifest)!=len(links) or len(links)!=len(post.media_items):
            raise ValueError('Prepared media manifest does not match selected assets')
        for entry,(link,asset) in zip(manifest,links,strict=True):
            if entry['source_asset_id']!=asset.id:
                raise ValueError('Prepared media selection changed')
            path=safe_path(root,entry['storage_key']) if entry.get('storage_key') else None
            if not entry.get('telegram_file_id') and (not path or not path.is_file()):
                raise ValueError('Prepared immutable file is missing')
            prepared.append(PreparedMedia(path=path,as_document=entry['as_document'],order=entry['order'],
                media_type=MediaType(entry['media_type']),telegram_file_id=entry.get('telegram_file_id')))
        return prepared
    # Compatibility for prepared plans created before media variants were introduced.
    if len(links)!=len(post.media_items):raise ValueError('Asset catalogue is incomplete')
    for (link,asset),source in zip(links,post.media_items,strict=True):
        path=safe_path(root,asset.storage_key) if asset.storage_key else None
        if not asset.telegram_file_id and (asset.state!='available' or not path or not path.is_file()):
            raise ValueError('Required original is missing')
        prepared.append(PreparedMedia(path=path,as_document=mode=='document' or source.media_type==MediaType.DOCUMENT,
            order=link.sort_order,media_type=source.media_type,telegram_file_id=asset.telegram_file_id))
    return prepared


def media_parts(manifest, links, caption, *, text_separate=False):
    """Mirror TelegramPublisher's grouping choices using immutable prepared flags."""
    if not manifest:return [{'kind':'text','html':caption}]
    result=[]
    has_documents=any(x['as_document'] or x['media_type']=='document' for x in manifest)
    send_as_documents=has_documents and all(not x.get('telegram_file_id') or x['as_document'] or x['media_type']=='document' for x in manifest)
    for offset in range(0,len(manifest),10):
        chunk=manifest[offset:offset+10]
        can_group=send_as_documents or all(x['media_type'] in {'image','video'} and not x['as_document'] for x in chunk)
        groups=[chunk] if len(chunk)>1 and can_group else [[x] for x in chunk]
        position=offset
        for group in groups:
            result.append({'kind':'media','method':'sendMediaGroup' if len(group)>1 else 'sendDocument' if (group[0]['as_document'] if len(manifest)==1 else send_as_documents) else 'sendVideo' if group[0]['media_type']=='video' else 'sendAnimation' if group[0]['media_type']=='animation' else 'sendPhoto',
                'asset_ids':[x['asset_id'] for x in group], 'source_indices':[links[i][0].source_index for i in range(position,position+len(group))],
                'caption_html':caption if not result and not text_separate else ''})
            position+=len(group)
    if text_separate:result.append({'kind':'text','html':caption})
    return result
