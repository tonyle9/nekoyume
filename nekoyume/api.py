import datetime

from flask import Blueprint, jsonify, request
import requests

from nekoyume.tasks import block_broadcast, move_broadcast
from nekoyume.models import db, Block, Node, Move, get_my_public_url


api = Blueprint('api', __name__, template_folder='templates')


@api.route('/ping')
def get_pong():
    return 'pong'


@api.route('/public_url')
def get_public_url():
    return jsonify(
        url=get_my_public_url()
    )


@api.route(Node.get_nodes_endpoint, methods=['GET'])
def get_nodes():
    nodes = Node.query.filter(
        Node.last_connected_at >= datetime.datetime.now() -
        datetime.timedelta(60 * 3)
    ).order_by(Node.last_connected_at.desc()).limit(2500).all()

    public_url = get_my_public_url()
    if public_url:
        nodes.append(Node(url=public_url,
                          last_connected_at=datetime.datetime.now()))

    return jsonify(nodes=[n.url for n in nodes])


@api.route(Node.post_node_endpoint, methods=['POST'])
def post_node():
    if 'url' in request.values:
        url = request.values['url']
    elif 'url' in request.get_json():
        url = request.get_json()['url']
    else:
        return jsonify(
            result='failed',
            message='Invalid parameter.'
        ), 400
    node = Node.query.get(url)
    if node:
        return jsonify(result='success')
    else:
        node = Node(url=url)
        db.session.add(node)
        try:
            response = requests.get(f'{node.url}/ping')
        except requests.exceptions.ConnectionError:
            return jsonify(
                result='failed',
                message=f'Connection to node {node.url} was failed.'
            ), 403
        if response.status_code == 200:
            node.last_connected_at = datetime.datetime.now()
            db.session.commit()
            return jsonify(result='success')
        else:
            return jsonify(
                result='failed',
                message=f'Connection to node {node.url} was failed.'
            ), 403


@api.route(Node.get_blocks_endpoint, methods=['GET'])
def get_blocks():
    last_block = Block.query.order_by(Block.id.desc()).first()
    from_ = request.values.get('from', 1, type=int)
    to = request.values.get(
        'to',
        last_block.id if last_block else 0,
        type=int)
    blocks = Block.query.filter(
        Block.id >= from_,
        Block.id <= to
    ).order_by(Block.id.asc())
    return jsonify(blocks=[b.serialize(use_bencode=False,
                                       include_suffix=True,
                                       include_moves=True,
                                       include_hash=True)
                           for b in blocks])


@api.route('/blocks/<string:block_hash>')
def get_block_by_hash(block_hash):
    block = Block.query.filter_by(hash=block_hash).first()
    if block:
        block = block.serialize(use_bencode=False,
                                include_suffix=True,
                                include_moves=True,
                                include_hash=True)
    return jsonify(block=block)


@api.route('/blocks/<int:block_id>')
def get_block_by_id(block_id):
    block = Block.query.get(block_id)
    if block:
        block = block.serialize(use_bencode=False,
                                include_suffix=True,
                                include_moves=True,
                                include_hash=True)
    return jsonify(block=block)


@api.route('/blocks/last')
def get_last_block():
    block = Block.query.order_by(Block.id.desc()).first()
    if block:
        block = block.serialize(use_bencode=False,
                                include_suffix=True,
                                include_moves=True,
                                include_hash=True)
    return jsonify(block=block)


@api.route('/moves/<string:move_id>')
def get_moves(move_id):
    move = Move.query.get(move_id)
    if move:
        move = move.serialize(False, True, True, True)
    return jsonify(move=move)


@api.route(Node.post_block_endpoint, methods=['POST'])
def post_block():
    new_block = request.get_json()
    last_block = Block.query.order_by(Block.id.desc()).first()

    if not new_block:
        return jsonify(result='failed',
                       message="empty block."), 400

    if not last_block and new_block['id'] != 1:
        Block.sync(
            Node.query.order_by(
                Node.last_connected_at.desc()).first())
        return jsonify(result='failed',
                       message="new block isn't our next block."), 403

    if (new_block['id'] > 1 and
       (new_block['id'] != last_block.id + 1 or
       new_block['prev_hash'] != last_block.hash)):
        if new_block['id'] > last_block.id + 1:
            Block.sync(
                Node.query.order_by(
                    Node.last_connected_at.desc()).first())
        return jsonify(result='failed',
                       message="new block isn't our next block."), 403

    block = Block()
    block.id = new_block['id']
    block.creator = new_block['creator']
    block.created_at = datetime.datetime.strptime(
        new_block['created_at'], '%Y-%m-%d %H:%M:%S.%f')
    block.prev_hash = new_block['prev_hash']
    block.hash = new_block['hash']
    block.difficulty = new_block['difficulty']
    block.suffix = new_block['suffix']
    block.root_hash = new_block['root_hash']

    for new_move in new_block['moves']:
        move = Move.query.get(new_move['id'])
        if not move:
            move = Move(
                id=new_move['id'],
                user=new_move['user'],
                name=new_move['name'],
                signature=new_move['signature'],
                tax=new_move['tax'],
                details=new_move['details'],
                created_at=datetime.datetime.strptime(
                    new_move['created_at'], '%Y-%m-%d %H:%M:%S.%f'
                ),
                block_id=block.id,
            )
        if not move.valid:
            return jsonify(result='failed',
                           message=f"move {move.id} isn't valid."), 400
        block.moves.append(move)
    if not block.valid:
        return jsonify(result='failed',
                       message="new block isn't valid."), 400

    db.session.add(block)
    db.session.commit()
    sent_node = Node()
    if 'sent_node' in new_block:
        sent_node.url = new_block['sent_node']
    block_broadcast.delay(
        block.id,
        sent_node_url=sent_node.url,
        my_node_url=f'{request.scheme}://{request.host}'
    )
    return jsonify(result='success')


@api.route(Node.post_move_endpoint, methods=['POST'])
def post_move():
    new_move = request.get_json()
    move = Move.query.get(new_move['id'])

    if move:
        return jsonify(result='success')

    if not move:
        move = Move(
            id=new_move['id'],
            user=new_move['user'],
            name=new_move['name'],
            signature=new_move['signature'],
            tax=new_move['tax'],
            details=new_move['details'],
            created_at=datetime.datetime.strptime(
                new_move['created_at'], '%Y-%m-%d %H:%M:%S.%f'
            ),
        )

    if not move.valid:
        return jsonify(result='failed',
                       message=f"move {move.id} isn't valid."), 400

    db.session.add(move)
    db.session.commit()

    sent_node = Node()
    if 'sent_node' in new_move:
        sent_node.url = new_move['sent_node']

    move_broadcast.delay(
        move.id,
        sent_node_url=sent_node.url,
        my_node_url=f'{request.scheme}://{request.host}'
    )
    return jsonify(result='success')
