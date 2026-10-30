import copy,unittest
import workflow_planner as p
import coordination_benchmark as b
import change_coordinator as c
import simulator as sim
class PlannerTests(unittest.TestCase):
    def state(self):return b.fixture({'id':'test','profile':'balanced'})[0]
    def items(self,v):return [p.request(i['actor'],{k:x for k,x in i.items() if k!='actor'}) for i in v['requests']]
    def test_all_profiles_are_typed_and_large_batches_are_not_called_globally_optimal(self):
        for name in ('balanced','bulk','dependency-chain','deployment-heavy'):
            workflow=p.profile(name);items=self.items(workflow);plan=p.plan(self.state(),items,at=1800000000)
            self.assertTrue(plan['selected']['reference_agrees']);self.assertLessEqual(plan['selected']['committed_units'],plan['selected']['capacity_limit'])
            self.assertEqual(plan['global_optimum_proven'],len(items)<=6)
            if len(items)>6:self.assertLessEqual(plan['orders_compared'],p.MAX_EVALUATIONS)
    def test_small_graph_matches_the_existing_exact_policy_baseline_without_prerequisites(self):
        state=self.state();intents=self.items(p.profile());old=c.plan_orders(state,intents,at=1800000000);new=p.plan(state,intents,at=1800000000)
        self.assertEqual(old['selected']['objective'],new['selected']['objective']);self.assertEqual(new['orders_compared'],120)
    def test_dependency_must_be_selected_before_a_child_can_act(self):
        items=self.items(p.profile('dependency-chain'));state=self.state();ids=[i['id'] for i in items];child='customer-route'
        out=p.evaluate(state,items,[child]+[i for i in ids if i!=child])
        self.assertNotIn(child,out['accepted']);self.assertEqual(next(w for w in out['waiting'] if w['id']==child)['code'],'PREREQUISITE_NOT_SELECTED')
    def test_cycles_unknown_parent_and_duplicate_ids_are_rejected(self):
        for kind in ('cycle','missing','duplicate'):
            v=p.profile()
            if kind=='cycle':v['requests'][0]['after']=[v['requests'][1]['id']];v['requests'][1]['after']=[v['requests'][0]['id']]
            if kind=='missing':v['requests'][0]['after']=['unknown']
            if kind=='duplicate':v['requests'][1]['id']=v['requests'][0]['id']
            with self.assertRaises(ValueError):p.validate_workflow(v)
    def test_parameters_cannot_add_tools_authority_or_bool_units(self):
        v=p.profile()
        for change in ({'tool':'shell'},{'approved':True},{'parameters':{'units':True}},{'actor':'RECOVERY'}):
            bad=copy.deepcopy(v);bad['requests'][0].update(change)
            with self.assertRaises(ValueError):p.validate_workflow(bad)
    def test_route_ownership_revocation_and_stale_observations_still_block(self):
        state=self.state();items=self.items(p.profile());state['approvals']['AP-route-C']['revoked']=True
        out=p.plan(state,items,at=1800000000)['selected'];self.assertNotIn('customer-route',out['accepted']);self.assertNotIn('customer-job',out['accepted'])
    def test_exact_order_cannot_drop_duplicate_or_invent_a_request(self):
        items=self.items(p.profile());ids=[i['id'] for i in items]
        for order in (ids[:-1],ids[:-1]+[ids[0]],ids[:-1]+['other']):
            with self.assertRaises(ValueError):p.evaluate(self.state(),items,order)
    def test_request_count_limit_and_duplicate_job_identity_are_checked(self):
        v=p.profile('bulk');v['requests']*=2
        with self.assertRaises(ValueError):p.validate_workflow(v)
        v=p.profile();v['requests'].append({**v['requests'][3],'id':'another-job'})
        with self.assertRaises(ValueError):p.validate_workflow(v)
if __name__=='__main__':unittest.main()
