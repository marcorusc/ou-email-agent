import unittest
from benchmarks.run import metrics, cluster_interval
from benchmarks.report import notification_metrics


def row(expected, predicted, error=None, scenario='one'):
    return {'expected_important':expected,'prediction':{'important':predicted,'category':'routine'},
            'acceptable_categories':['routine'],'error':error,'raw_important':predicted,'scenario':scenario}


class BenchmarkMetricsTests(unittest.TestCase):
    def test_rejected_predictions_are_not_dropped(self):
        rows=[row(True,True),row(True,True,error={'type':'ValueError'}),row(False,False),row(False,True)]
        result=metrics(rows)
        self.assertEqual(result['strict_accuracy'],.5)
        self.assertEqual(result['alert_recall'],.5)
        self.assertEqual(result['alert_precision'],.5)
        self.assertEqual(result['missed_important'],1)
        self.assertEqual(result['false_alerts'],1)
        self.assertEqual(result['validation_or_request_failures'],1)
        self.assertEqual(result['raw_importance_accuracy'],.75)

    def test_undefined_precision_is_not_perfect(self):
        result=metrics([row(True,False)])
        self.assertIsNone(result['alert_precision'])
        self.assertEqual(result['alert_recall'],0)
        self.assertEqual(result['alert_f1'],0)

    def test_cluster_interval_preserves_perfect_scores(self):
        self.assertEqual(cluster_interval([row(True,True),row(False,False)],100),[1,1])

    def test_notification_filter_excludes_announcement_category(self):
        announcement=row(False,True)
        announcement['prediction']['category']='announcement'
        request=row(True,True)
        request['prediction']['category']='direct_request'
        result=notification_metrics([announcement,request])
        self.assertEqual(result['false_positive'],0)
        self.assertEqual(result['recall'],1)

if __name__=='__main__': unittest.main()
